"""Per-(symbol, trade_date) tradability panel for A-shares.

The backtest engine could previously answer three questions only by guessing:

* **Is it halted?** - no signal at all. A suspended symbol simply had no bar,
  so the engine carried the last close and "traded" a stock nobody could trade.
* **Where is the limit?** - guessed from the close-to-close move, with the band
  derived from the code prefix. ``_price_limit`` documents its own blind spot:
  ST stocks move in a 5% band and the prefix cannot reveal that.
* **Was it ST?** - unknown, so ST names were treated as 10% names.

This module builds the panel that replaces the guesses. It is a pure function
over bars you already have plus a universe snapshot; the network side (fetching
the universe and the bars) lives in ``scripts/build_tradability.py`` so the
panel logic stays testable offline.

Panel schema (one row per symbol x session)::

    symbol  date  is_suspended  is_st  board  pre_close
    limit_up  limit_down  band_mismatch  close  volume  amount

Rows exist for *halted* sessions too - that is the whole point. A session with
no bar inside a symbol's listed window is written as ``is_suspended=True``.

Price bands need *unadjusted* prices
------------------------------------
The exchange applies the band to the previous **unadjusted** close. Several
loaders (Tencent, and AKShare's qfq endpoints) return forward-adjusted prices,
where a dividend or split makes the close-to-close ratio jump outside the band.
``band_mismatch`` marks exactly those rows: the computed band contradicts the
observed move. Consumers must not trust the band on those rows - the engine
keeps the halt flag and falls back to its heuristic for the band instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "DATASET",
    "board_of",
    "price_limit_pct",
    "limit_prices",
    "build_panel",
    "save_panel",
    "load_panel",
    "TradabilityIndex",
]

DATASET = "tradability"

# Exchange price bands. ST names are 5% on both boards; the Beijing exchange
# runs 30%; ChiNext/STAR 20% since the 2020 reform.
_LIMIT_MAIN = 0.10
_LIMIT_GROWTH = 0.20
_LIMIT_BSE = 0.30
_LIMIT_ST = 0.05

_COLUMNS = [
    "symbol",
    "date",
    "is_suspended",
    "is_st",
    "board",
    "pre_close",
    "limit_up",
    "limit_down",
    "band_mismatch",
    "close",
    "volume",
    "amount",
]

# A close may sit one tick either side of the computed band (rounding) before
# the row is treated as a genuine contradiction.
_BAND_TOLERANCE = 0.011


# ---------------------------------------------------------------------------
# Board taxonomy and price bands
# ---------------------------------------------------------------------------


def board_of(symbol: str) -> str:
    """Classify an A-share code into its board.

    Args:
        symbol: Code with or without exchange suffix (``300750.SZ`` / ``300750``).

    Returns:
        One of ``main``, ``chinext``, ``star``, ``bse``.
    """
    code = symbol.split(".", 1)[0].strip()
    suffix = symbol.split(".", 1)[1].strip().upper() if "." in symbol else ""
    if suffix == "BJ":
        return "bse"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith("688"):
        return "star"
    # Beijing: 8xxxxx (old NEEQ select tier), 4xxxxx, and the 920xxx range the
    # exchange moved new listings to.  Leaving 92 out classified the whole
    # renumbered cohort as ``main`` and gave it a 10% band it never had — every
    # 920xxx row in September 2026 was flagged as a band mismatch.
    if len(code) == 6 and code.startswith(("8", "4", "92")):
        return "bse"
    return "main"


def price_limit_pct(symbol: str, *, is_st: bool = False) -> float:
    """Return the daily price band for ``symbol`` as a fraction.

    ST names always take 5% regardless of board - the rule the old helper
    admitted it could not detect.

    Args:
        symbol: A-share code.
        is_st: Whether the name carries an ST/*ST designation on that date.

    Returns:
        ``0.05``, ``0.10``, ``0.20`` or ``0.30``.
    """
    if is_st:
        return _LIMIT_ST
    board = board_of(symbol)
    if board in {"chinext", "star"}:
        return _LIMIT_GROWTH
    if board == "bse":
        return _LIMIT_BSE
    return _LIMIT_MAIN


def _round_cent(value: float) -> float:
    """Round to the exchange's 0.01 tick using half-up, not banker's rounding."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_prices(
    pre_close: float | None, symbol: str, *, is_st: bool = False
) -> tuple[float | None, float | None]:
    """Return ``(limit_up, limit_down)`` prices for one session.

    Args:
        pre_close: Previous close (the base the exchange applies the band to).
        symbol: A-share code.
        is_st: ST designation on that date.

    Returns:
        ``(None, None)`` when ``pre_close`` is missing or non-positive.
    """
    if pre_close is None or not (pre_close > 0):
        return None, None
    band = price_limit_pct(symbol, is_st=is_st)
    return _round_cent(pre_close * (1 + band)), _round_cent(pre_close * (1 - band))


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------


def build_panel(
    bars: Mapping[str, pd.DataFrame],
    *,
    universe: pd.DataFrame | None = None,
    sessions: Sequence[object] | None = None,
) -> pd.DataFrame:
    """Build the tradability panel from bars the caller already fetched.

    Args:
        bars: ``symbol -> DataFrame`` with at least ``date``, ``close`` and
            ``volume``; ``pre_close`` and ``amount`` are used when present.
            When ``pre_close`` is missing it is derived from the previous bar,
            which makes the first bar of each symbol limit-less (it has no
            base) - those rows are written with null limits rather than a guess.
        universe: Optional frame with ``symbol`` plus optional ``is_st`` and
            ``board`` columns.  Missing ``is_st`` is treated as False.
        sessions: Optional session list (``YYYY-MM-DD`` or dates).  When given,
            every symbol is expanded across these sessions so halted days get
            explicit rows.  When omitted, only the symbol's own bar dates are
            used and halts are inferred from zero volume.

    Returns:
        Panel frame with the schema documented in the module docstring.
    """
    if not bars:
        return pd.DataFrame(columns=_COLUMNS)

    session_list = _normalize_sessions(sessions)
    st_map, board_map = _universe_maps(universe)

    frames: list[pd.DataFrame] = []
    for symbol, raw in bars.items():
        if raw is None or raw.empty:
            continue
        frame = _normalize_bars(raw, symbol)
        if frame.empty:
            continue

        if session_list:
            index = [
                d
                for d in session_list
                if frame.index.min() <= d <= frame.index.max()
            ]
            if not index:
                continue
            frame = frame.reindex(index)

        is_st = bool(st_map.get(symbol, False))
        board = board_map.get(symbol) or board_of(symbol)

        pre_close = _pre_close_series(frame)
        volume = frame["volume"] if "volume" in frame.columns else pd.Series(index=frame.index, dtype=float)
        amount = frame["amount"] if "amount" in frame.columns else pd.Series(index=frame.index, dtype=float)

        # A row with no close or zero volume is a halt: the exchange publishes
        # no trade, so no price exists for that session.
        suspended = frame["close"].isna() | (volume.fillna(0) <= 0)

        out = pd.DataFrame(
            {
                "symbol": symbol,
                "date": [d.isoformat() for d in frame.index],
                "is_suspended": suspended.to_numpy(),
                "is_st": is_st,
                "board": board,
                "pre_close": pre_close.to_numpy(),
                "close": frame["close"].to_numpy(),
                "volume": volume.to_numpy(),
                "amount": amount.to_numpy(),
            }
        )
        limits = [_limits_row(pc, symbol, is_st) for pc in out["pre_close"]]
        out["limit_up"] = [up for up, _ in limits]
        out["limit_down"] = [down for _, down in limits]
        out["band_mismatch"] = _band_mismatch(out)
        frames.append(out)

    if not frames:
        return pd.DataFrame(columns=_COLUMNS)

    panel = pd.concat(frames, ignore_index=True)
    panel = panel[_COLUMNS].sort_values(["date", "symbol"], kind="stable").reset_index(drop=True)
    return panel


def _limits_row(pre_close, symbol: str, is_st: bool) -> tuple[float | None, float | None]:
    try:
        value = None if pd.isna(pre_close) else float(pre_close)
    except (TypeError, ValueError):
        value = None
    return limit_prices(value, symbol, is_st=is_st)


def _band_mismatch(out: pd.DataFrame) -> pd.Series:
    """Flag rows whose observed close contradicts the computed band.

    Fires on adjusted price series (a dividend inside the window moves the
    close-to-close ratio outside the band) and on corrupt bars. The band is not
    trustworthy for those rows, so consumers must fall back to the heuristic.
    """
    close = pd.to_numeric(out["close"], errors="coerce")
    up = pd.to_numeric(out["limit_up"], errors="coerce")
    down = pd.to_numeric(out["limit_down"], errors="coerce")
    above = close > up + _BAND_TOLERANCE
    below = close < down - _BAND_TOLERANCE
    return (above | below).fillna(False).astype(bool)


def _normalize_bars(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Coerce one symbol's bars into a date-indexed, lower-cased frame.

    Loaders disagree on where the date lives: some return a ``date`` column,
    Tencent a ``DatetimeIndex`` named ``trade_date``, Tushare a ``trade_date``
    column. All three are accepted here.
    """
    frame = raw.copy()
    frame.columns = [str(c).lower() for c in frame.columns]

    if "date" not in frame.columns:
        for candidate in ("trade_date", "datetime", "time", "day"):
            if candidate in frame.columns:
                frame = frame.rename(columns={candidate: "date"})
                break
    if "date" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
            # reset_index names the column after the index ("trade_date"),
            # which the rename above already ran past.
            frame = frame.rename(columns={frame.columns[0]: "date"})
        else:
            raise ValueError(f"bars for {symbol!r} need a 'date' column or a DatetimeIndex")

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame["_day"] = frame["date"].dt.date
    frame = frame.drop_duplicates(subset=["_day"], keep="last").set_index("_day").sort_index()
    if "close" not in frame.columns:
        frame["close"] = pd.NA
    return frame


def _pre_close_series(frame: pd.DataFrame) -> pd.Series:
    """Use the source's ``pre_close`` when present, else derive from the prior bar."""
    if "pre_close" in frame.columns:
        supplied = pd.to_numeric(frame["pre_close"], errors="coerce")
        derived = pd.to_numeric(frame["close"], errors="coerce").shift(1)
        return supplied.where(supplied.notna(), derived)
    return pd.to_numeric(frame["close"], errors="coerce").shift(1)


def _normalize_sessions(sessions: Sequence[object] | None) -> list[dt.date]:
    if not sessions:
        return []
    out: list[dt.date] = []
    for value in sessions:
        if isinstance(value, dt.datetime):
            out.append(value.date())
        elif isinstance(value, dt.date):
            out.append(value)
        else:
            out.append(pd.Timestamp(value).date())
    return sorted(set(out))


def _universe_maps(
    universe: pd.DataFrame | None,
) -> tuple[dict[str, bool], dict[str, str]]:
    """Pull the ``is_st`` / ``board`` lookups out of the universe snapshot."""
    st_map: dict[str, bool] = {}
    board_map: dict[str, str] = {}
    if universe is None or universe.empty:
        return st_map, board_map
    frame = universe.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "symbol" not in frame.columns:
        return st_map, board_map
    if "is_st" in frame.columns:
        st_map = dict(zip(frame["symbol"], frame["is_st"].fillna(False).astype(bool)))
    if "board" in frame.columns:
        board_map = {s: b for s, b in zip(frame["symbol"], frame["board"]) if isinstance(b, str)}
    return st_map, board_map


# ---------------------------------------------------------------------------
# Persistence and lookup
# ---------------------------------------------------------------------------


def save_panel(panel: pd.DataFrame) -> int:
    """Upsert the panel into the lake.  Returns the row count written."""
    from src.data.lake import write_dataset

    frame = panel.copy()
    if "date" in frame.columns:
        frame["date"] = frame["date"].astype(str)
    return write_dataset(frame, DATASET, key=("symbol", "date"), sort_by=("date", "symbol"))


def load_panel(
    *,
    start: str | None = None,
    end: str | None = None,
    symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Load the cached panel, optionally narrowed by date/symbol.

    Returns an empty frame when the panel has never been built - callers are
    expected to fall back to the heuristic path rather than fail.
    """
    from src.data.lake import read_dataset

    filters = None
    if start and end:
        try:
            first, last = int(str(start)[:4]), int(str(end)[:4])
            filters = {"year": list(range(first, last + 1))}
        except ValueError:
            filters = None

    frame = read_dataset(DATASET, filters=filters)
    if frame.empty:
        return frame
    if start and "date" in frame.columns:
        frame = frame[frame["date"].astype(str) >= str(start)]
    if end and "date" in frame.columns:
        frame = frame[frame["date"].astype(str) <= str(end)]
    if symbols is not None and "symbol" in frame.columns:
        wanted = set(symbols)
        frame = frame[frame["symbol"].isin(wanted)]
    return frame.reset_index(drop=True)


class TradabilityIndex:
    """O(1) ``(symbol, date) -> row`` lookups for the backtest engine.

    The engine calls :meth:`get` once per candidate order.  A dict keyed on the
    pair is an order of magnitude cheaper than a pandas boolean mask, and the
    per-day usage pattern makes a lazy per-date cache pointless.
    """

    def __init__(self, panel: pd.DataFrame):
        self._rows: dict[tuple[str, str], dict[str, object]] = {}
        if panel is None or panel.empty:
            return
        for record in panel.to_dict(orient="records"):
            key = (str(record.get("symbol")), str(record.get("date")))
            self._rows[key] = record

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, symbol: str, day) -> dict[str, object] | None:
        """Return the panel row for ``symbol`` on ``day``, or None when unknown."""
        if day is None:
            return None
        if isinstance(day, (dt.datetime, pd.Timestamp)):
            day = day.date()
        key = (symbol, day.isoformat() if isinstance(day, dt.date) else str(day))
        return self._rows.get(key)
