"""A-share risk markers for monitor suggestions.

Markers come from bars the scan already loaded, plus a tradability-panel row
when one exists for that name and session. Nothing here calls the network.
A provisional (live-quote) bar is left unmarked: its OHLC is often a single
print and would look like a one-word board.

A **buy** that carries ``st``, ``limit_up``, ``one_word``, or ``extreme_board``
is not tradable. A **sell** keeps the same flags and stays eligible: the
marker is there so a limit-up or ST name can still be exited.

``extreme_board`` is this name's own limit-up streak (three or more sessions
ending on the bar in use). It is not a market-wide sentiment index. That
series is not on the bar panel, so it is not invented here.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import pandas as pd

from src.data.tradability import limit_prices
from src.monitor.models import MonitorSuggestion

logger = logging.getLogger(__name__)

# Buy-side blocks. ``consecutive_board`` (a streak of two) is recorded and
# shown; the third board is the hard sentiment block.
BUY_BLOCK_FLAGS = frozenset({"st", "limit_up", "one_word", "extreme_board"})
CONSECUTIVE_STREAK = 2
EXTREME_STREAK = 3
# Same price tolerance the paper-book fill path uses against the panel band.
_PRICE_EPS = 0.005


def buy_block_reasons(flags: list[str] | None) -> list[str]:
    """Stable admission reason codes for buy-side markers."""
    reasons: list[str] = []
    for flag in flags or []:
        if flag in BUY_BLOCK_FLAGS and f"ashare_{flag}" not in reasons:
            reasons.append(f"ashare_{flag}")
    return reasons


def stamp_suggestions(
    suggestions: list[MonitorSuggestion],
    frames: Mapping[str, pd.DataFrame] | None,
    *,
    as_of: str,
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[MonitorSuggestion]:
    """Attach ``risk_flags`` from ``frames``. Unknown symbols stay unmarked."""
    if not suggestions or not frames:
        return suggestions
    by_symbol = {_norm(key): frame for key, frame in frames.items()}
    rows_by_symbol = {symbol: _frame_rows(frame) for symbol, frame in by_symbol.items()}
    panel_rows = panel if panel is not None else _load_panel(list(by_symbol), as_of)
    for suggestion in suggestions:
        # A provisional bar is a live quote painted onto the session. Its OHLC
        # is often just the last print, which looks like a one-word board.
        # Settled bars are the ones that can block a buy. Paper book already
        # refuses provisional signals.
        if suggestion.provisional:
            suggestion.risk_flags = []
            continue
        symbol = _norm(suggestion.symbol)
        rows = rows_by_symbol.get(symbol) or []
        suggestion.risk_flags = assess_flags(symbol, rows, as_of=as_of, panel=panel_rows)
    return suggestions


def assess_flags(
    symbol: str,
    rows: list[dict[str, Any]],
    *,
    as_of: str,
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[str]:
    """Flags for the last bar on or before ``as_of``.

    A missing open/high/low does not invent a one-word board. A missing ST
    flag does not invent an ST name.
    """
    usable = [row for row in rows if row.get("date") and str(row["date"]) <= as_of]
    if not usable:
        return []
    latest = usable[-1]
    streak = 0
    for row in reversed(usable):
        if _is_limit_up(symbol, row, panel):
            streak += 1
        else:
            break
    flags: list[str] = []
    if _is_st(symbol, latest, panel):
        flags.append("st")
    if _is_limit_up(symbol, latest, panel):
        flags.append("limit_up")
    if _is_one_word(symbol, latest, panel):
        flags.append("one_word")
    if streak >= CONSECUTIVE_STREAK:
        flags.append("consecutive_board")
    if streak >= EXTREME_STREAK:
        flags.append("extreme_board")
    return flags


def _is_st(
    symbol: str,
    row: dict[str, Any],
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> bool:
    panel_row = _panel_row(symbol, row.get("date"), panel)
    if panel_row is not None and _as_bool(panel_row.get("is_st")):
        return True
    if _as_bool(row.get("is_st")):
        return True
    return _name_is_st(row.get("name"))


def _name_is_st(name: Any) -> bool:
    text = str(name or "").strip().upper().replace(" ", "")
    if not text:
        return False
    return text.startswith("*ST") or text.startswith("ST") or text.startswith("S*ST")


def _is_limit_up(
    symbol: str,
    row: dict[str, Any],
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> bool:
    if _is_suspended(symbol, row, panel):
        return False
    close = _positive(row.get("close"))
    ceiling = _ceiling(symbol, row, panel)
    if close is None or ceiling is None:
        return False
    return close >= ceiling - _PRICE_EPS


def _is_one_word(
    symbol: str,
    row: dict[str, Any],
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> bool:
    """Opened at the ceiling and never traded below it."""
    if not _is_limit_up(symbol, row, panel):
        return False
    ceiling = _ceiling(symbol, row, panel)
    if ceiling is None:
        return False
    prices = [_positive(row.get(key)) for key in ("open", "high", "low")]
    if any(price is None for price in prices):
        return False
    return all(price >= ceiling - _PRICE_EPS for price in prices if price is not None)


def _ceiling(
    symbol: str,
    row: dict[str, Any],
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> float | None:
    panel_row = _panel_row(symbol, row.get("date"), panel)
    if panel_row is not None and not _as_bool(panel_row.get("band_mismatch")):
        official = _positive(panel_row.get("limit_up"))
        if official is not None:
            return official
    pre = _positive(row.get("pre_close"))
    if pre is None:
        return None
    up, _down = limit_prices(pre, symbol, is_st=_is_st(symbol, row, panel))
    return up


def _is_suspended(
    symbol: str,
    row: dict[str, Any],
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> bool:
    panel_row = _panel_row(symbol, row.get("date"), panel)
    if panel_row is not None and _as_bool(panel_row.get("is_suspended")):
        return True
    volume = row.get("volume")
    if volume is None:
        return False
    try:
        return float(volume) <= 0
    except (TypeError, ValueError):
        return False


def _panel_row(
    symbol: str,
    day: Any,
    panel: Mapping[tuple[str, str], Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    if not panel or not day:
        return None
    text = str(day)
    for key in (_norm(symbol), _norm(symbol).split(".", 1)[0]):
        found = panel.get((key, text))
        if found is not None:
            return found
    return None


def _load_panel(symbols: list[str], as_of: str) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Best-effort panel slice. An empty lake or a read error leaves no rows."""
    if not symbols:
        return {}
    try:
        from src.config.accessor import get_env_config

        if not get_env_config().data.vibe_trading_tradability_panel:
            return {}
        from src.data.tradability import load_panel

        start = (pd.Timestamp(as_of) - pd.DateOffset(days=40)).strftime("%Y-%m-%d")
        frame = load_panel(start=start, end=as_of, symbols=symbols)
    except Exception as exc:  # noqa: BLE001 — markers still work off the bars
        logger.info("tradability panel unavailable for risk flags: %s", exc)
        return {}
    if frame is None or getattr(frame, "empty", True):
        return {}
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in frame.to_dict(orient="records"):
        symbol = _norm(record.get("symbol"))
        day = str(record.get("date") or "")[:10]
        if symbol and day:
            out[(symbol, day)] = record
    return out


def _frame_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    rows: list[dict[str, Any]] = []
    previous_close: float | None = None
    ordered = frame.sort_index()
    for idx, series in ordered.iterrows():
        day = _row_date(idx, series)
        if day is None:
            continue
        pre = _positive(_cell(series, "pre_close"))
        if pre is None:
            pre = previous_close
        close = _positive(_cell(series, "close"))
        rows.append(
            {
                "date": day,
                "open": _cell(series, "open"),
                "high": _cell(series, "high"),
                "low": _cell(series, "low"),
                "close": close,
                "volume": _cell(series, "volume"),
                "pre_close": pre,
                "is_st": _cell(series, "is_st"),
                "name": _cell(series, "name"),
            }
        )
        if close is not None:
            previous_close = close
    return rows


def _row_date(idx: Any, series: pd.Series) -> str | None:
    for candidate in (_cell(series, "trade_date"), _cell(series, "date"), idx):
        text = _date_text(candidate)
        if text:
            return text
    return None


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.date().isoformat()


def _cell(series: pd.Series, key: str) -> Any:
    if key not in getattr(series, "index", []):
        return None
    value = series[key]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return value
    return value


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN
        return None
    return number


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    try:
        if value is None or pd.isna(value):
            return False
    except (TypeError, ValueError):
        return bool(value)
    return bool(value)


def _norm(symbol: Any) -> str:
    return str(symbol or "").strip().upper()
