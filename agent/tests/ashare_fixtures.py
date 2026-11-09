"""``fx_small`` — the deterministic A-share panel the risk and fill tests share.

Five names over 60 sessions, each carrying one hazard the monitor must handle:

======  ==================  ====================================================
symbol  hazard              what it pins
======  ==================  ====================================================
600519  clean trend         no marker; the control
600001  ``*ST`` name        5% band, ST excluded from buys
600002  halted 3 sessions   zero volume is not a limit-up; no block flags
600003  one-word board      opened at the ceiling and never traded below it
600004  three-board run     ``consecutive_board`` + ``extreme_board``
======  ==================  ====================================================

The panel is built, not loaded: no lake, no network, same numbers on every
machine. Prices are rounded the way an exchange rounds a band, so ``close``
lands exactly on the ceiling instead of a float hair below it.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd

BAR_COLUMNS = ("open", "high", "low", "close", "volume", "pre_close", "is_st", "name")

# name, hazard
SYMBOLS: dict[str, tuple[str, str]] = {
    "600519.SH": ("贵州茅台", "clean"),
    "600001.SH": ("*ST示例", "st"),
    "600002.SH": ("停牌示例", "halt"),
    "600003.SH": ("一字示例", "one_word"),
    "600004.SH": ("连板示例", "streak"),
}

# Flags ``assess_flags`` must return on the final session of each series.
EXPECTED_FLAGS: dict[str, list[str]] = {
    "600519.SH": [],
    "600001.SH": ["st"],
    "600002.SH": [],
    "600003.SH": ["limit_up", "one_word"],
    "600004.SH": ["limit_up", "consecutive_board", "extreme_board"],
}

_DEFAULT_END = "2024-08-23"


def _round_cent(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def band(pre_close: float, *, is_st: bool = False) -> float:
    """The ceiling an exchange would publish for ``pre_close``."""
    return _round_cent(pre_close * (1.05 if is_st else 1.10))


def sessions(count: int = 60, *, end: str = _DEFAULT_END) -> list[dt.date]:
    """Weekdays ending at ``end``, oldest first.

    Holidays are deliberately not removed: this is a bar panel, not a calendar
    fixture. Anything that needs the real calendar reads ``src.data.calendar``.
    """
    end_d = dt.date.fromisoformat(end)
    out: list[dt.date] = []
    cur = end_d
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return sorted(out)


def _row(day: dt.date, *, close: float, pre: float | None, open_: float, high: float,
         low: float, volume: float, is_st: bool, name: str) -> dict[str, Any]:
    return {
        "date": pd.Timestamp(day),
        "open": round(open_, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "close": round(close, 2),
        "volume": volume,
        "pre_close": None if pre is None else round(pre, 2),
        "is_st": is_st,
        "name": name,
    }


def _trend(day: dt.date, pre: float, *, drift: float, is_st: bool, name: str) -> dict[str, Any]:
    close = _round_cent(pre * (1 + drift))
    return _row(
        day,
        close=close,
        pre=pre,
        open_=pre * 1.001,
        high=max(pre, close) * 1.004,
        low=min(pre, close) * 0.996,
        volume=1_000_000,
        is_st=is_st,
        name=name,
    )


def _sealed(day: dt.date, pre: float, *, is_st: bool, name: str, volume: float = 80_000,
            sealed: bool = True) -> dict[str, Any]:
    """A session that closed at the ceiling; ``sealed`` also pins open/high/low."""
    ceiling = band(pre, is_st=is_st)
    return _row(
        day,
        close=ceiling,
        pre=pre,
        open_=ceiling if sealed else pre * 1.02,
        high=ceiling,
        low=ceiling if sealed else pre * 1.01,
        volume=volume,
        is_st=is_st,
        name=name,
    )


def _halted(day: dt.date, pre: float, *, is_st: bool, name: str) -> dict[str, Any]:
    """A suspension carries the previous close with zero volume."""
    return _row(
        day,
        close=pre,
        pre=pre,
        open_=pre,
        high=pre,
        low=pre,
        volume=0,
        is_st=is_st,
        name=name,
    )


def _series(symbol: str, days: list[dt.date], hazard: str) -> pd.DataFrame:
    name, _ = SYMBOLS[symbol]
    is_st = hazard == "st"
    rows: list[dict[str, Any]] = []
    pre: float | None = None
    for offset, day in enumerate(days):
        base = 10.0 if symbol != "600519.SH" else 1680.0
        drift = 0.003 if hazard == "clean" else 0.001
        if pre is None:
            close = base if offset == 0 else pre
            rows.append(
                _row(day, close=close, pre=None, open_=base * 0.999, high=base * 1.002,
                     low=base * 0.998, volume=1_000_000, is_st=is_st, name=name)
            )
            pre = rows[-1]["close"]
            continue

        remaining = len(days) - offset
        if hazard == "halt" and remaining <= 3:
            row = _halted(day, pre, is_st=is_st, name=name)
        elif hazard == "one_word" and remaining == 1:
            row = _sealed(day, pre, is_st=is_st, name=name, sealed=True)
        elif hazard == "streak" and remaining <= 3:
            row = _sealed(day, pre, is_st=is_st, name=name, sealed=False)
        else:
            row = _trend(day, pre, drift=drift, is_st=is_st, name=name)
        rows.append(row)
        pre = row["close"]
    return pd.DataFrame(rows).set_index("date")


def build(count: int = 60, *, end: str = _DEFAULT_END) -> dict[str, pd.DataFrame]:
    """Return ``{symbol: frame}`` for the whole panel."""
    days = sessions(count, end=end)
    return {symbol: _series(symbol, days, hazard) for symbol, (_, hazard) in SYMBOLS.items()}


def as_of(count: int = 60, *, end: str = _DEFAULT_END) -> str:
    """The session the panel ends on, as ``YYYY-MM-DD``."""
    return sessions(count, end=end)[-1].isoformat()
