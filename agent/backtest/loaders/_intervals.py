"""Bar-interval vocabulary shared by every loader.

Two problems motivate this module.

**Silent degradation.** A loader that only understands daily bars used to
accept ``interval="5m"`` and return daily rows tagged as 5-minute bars: the
cache key included the interval, so the frame looked legitimate, the engine
annualised it with 48 bars per day, and every metric after that was wrong.
``supports_interval`` gives the registry a way to *skip* a loader that cannot
serve the requested granularity instead of letting it answer the wrong
question.

**Duplicated resampling.** ``local_loader`` had a private ``_RESAMPLE_RULES``
table because a file on disk can hold any native granularity. Loaders that
fetch a finer bar than requested (60-minute bars for a ``4H`` request) need the
same logic, so it lives here once.

Resampling is anchored to each *session*, not to wall-clock midnight: an
A-share session is 09:30-11:30 plus 13:00-15:00, so ``4H`` must produce one bar
per session rather than two odd halves straddling the lunch break.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_INTERVALS",
    "DAILY_INTERVALS",
    "INTRADAY_INTERVALS",
    "normalize_interval",
    "is_intraday",
    "interval_minutes",
    "supports_interval",
    "native_interval",
    "resample_ohlcv",
    "clip_window",
]

#: Every interval the backtest config accepts (see ``runner._VALID_INTERVALS``).
CANONICAL_INTERVALS: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "1H", "4H", "1D"}
)

#: Intervals a daily-only loader can legitimately serve.
DAILY_INTERVALS: frozenset[str] = frozenset({"1D"})

#: Intervals finer than one session.
INTRADAY_INTERVALS: frozenset[str] = frozenset({"1m", "5m", "15m", "30m", "1H", "4H"})

#: Minutes per bar.  ``1D`` is one A-share session (4 hours of continuous
#: trading); ``None`` means "not a fixed duration".
_MINUTES: dict[str, int | None] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "4H": 240,
    "1D": 240,
    "1W": None,
    "1M": None,
}

# Exact-token aliases first (so ``"1M"`` stays monthly and ``"1m"`` stays
# minute — case matters for that one pair), then case-insensitive fallbacks.
_EXACT_ALIASES: dict[str, str] = {
    "1M": "1M",
    "1W": "1W",
    "D": "1D",
    "W": "1W",
}
_LOOSE_ALIASES: dict[str, str] = {
    "1m": "1m",
    "1min": "1m",
    "1minute": "1m",
    "min": "1m",
    "5m": "5m",
    "5min": "5m",
    "15m": "15m",
    "15min": "15m",
    "30m": "30m",
    "30min": "30m",
    "1h": "1H",
    "60m": "1H",
    "60min": "1H",
    "1hour": "1H",
    "4h": "4H",
    "240m": "4H",
    "1d": "1D",
    "d": "1D",
    "day": "1D",
    "daily": "1D",
    "1w": "1W",
    "week": "1W",
    "weekly": "1W",
    "month": "1M",
    "monthly": "1M",
    "1mo": "1M",
}


def normalize_interval(interval: Any) -> str:
    """Map an interval spelling onto the canonical token.

    Args:
        interval: Interval as configured or passed by a caller, e.g. ``"1d"``,
            ``"60m"``, ``"5min"``.

    Returns:
        A member of :data:`CANONICAL_INTERVALS` when the spelling is known,
        otherwise the trimmed input unchanged (callers treat that as
        unsupported rather than guessing).
    """
    token = str(interval or "1D").strip()
    if not token:
        return "1D"
    if token in CANONICAL_INTERVALS:
        return token
    if token in _EXACT_ALIASES:
        return _EXACT_ALIASES[token]
    return _LOOSE_ALIASES.get(token.lower(), token)


def is_intraday(interval: Any) -> bool:
    """Return True when *interval* is finer than one session."""
    return normalize_interval(interval) in INTRADAY_INTERVALS


def interval_minutes(interval: Any) -> int | None:
    """Return minutes per bar, or ``None`` for durations that are not fixed."""
    return _MINUTES.get(normalize_interval(interval))


def supports_interval(loader: Any, interval: Any) -> bool:
    """Whether *loader* (class or instance) can serve *interval* natively.

    A loader advertises its granularity through the ``SUPPORTED_INTERVALS``
    class attribute, and that declaration is authoritative: a loader that
    declares ``{"1D"}`` is skipped when an intraday bar is requested instead of
    being asked to answer the wrong question.

    Loaders without the attribute are treated as *unknown*, not as daily-only.
    Most of the registry validates ``interval`` itself (OKX/BaoStock-style
    rejections, Tushare's minute dispatch, yfinance's own mapping), and
    defaulting to daily-only would silently remove the crypto and US intraday
    paths from the fallback chain.  Declaring the attribute is how a loader
    opts into being filtered.

    Args:
        loader: Loader class or instance.
        interval: Requested bar size.

    Returns:
        False only when the loader declares a set that excludes *interval*.
    """
    declared = getattr(loader, "SUPPORTED_INTERVALS", None)
    if declared is None:
        return True
    wanted = normalize_interval(interval)
    return any(normalize_interval(item) == wanted for item in declared)


def native_interval(frame: pd.DataFrame) -> str | None:
    """Infer the bar size of *frame* from the median timestamp gap.

    Only intraday granularities are inferred; a daily frame returns ``"1D"``
    and anything unreadable returns ``None``.
    """
    if len(frame.index) < 3:
        return None
    deltas = pd.Series(frame.index).diff().dropna()
    gap = deltas[deltas > pd.Timedelta(0)].median()
    if pd.isna(gap):
        return None
    minutes = gap.total_seconds() / 60
    for token, size in (("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30), ("1H", 60)):
        if abs(minutes - size) < 0.5:
            return token
    return "1D" if minutes >= 1200 else None


_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


def resample_ohlcv(
    frame: pd.DataFrame, interval: str, *, symbol: str = "",
    label: str = "end",
) -> pd.DataFrame:
    """Aggregate *frame* up to a coarser *interval*.

    Bars are bucketed relative to the first bar observed in each session, so a
    lunch break never splits a bucket in the middle.  Bucket labels follow the
    bar-end convention Chinese feeds use (a 5-minute bar stamped 09:35 covers
    09:30-09:35), which keeps ``close`` known at the label time and avoids
    introducing look-ahead.

    Args:
        frame: OHLCV frame with a DatetimeIndex.
        interval: Target bar size.
        symbol: Symbol name, used only in log messages.
        label: ``"end"`` (default) stamps the bucket with its last timestamp,
            ``"start"`` with its first.

    Returns:
        The aggregated frame, or *frame* unchanged when it is already at (or
        coarser than) the target — upsampling cannot be invented, so the source
        bars are returned with a warning.
    """
    target = normalize_interval(interval)
    if frame is None or frame.empty:
        return frame
    if not isinstance(frame.index, pd.DatetimeIndex):
        logger.warning("resample_ohlcv: %s has a non-datetime index; returning as-is", symbol)
        return frame

    target_minutes = interval_minutes(target)
    source = native_interval(frame)
    source_minutes = interval_minutes(source) if source else None

    if target_minutes is None:
        logger.warning(
            "resample_ohlcv: %s is not a fixed-duration interval; returning %s bars",
            target, symbol,
        )
        return frame
    if source_minutes is not None:
        if target_minutes == source_minutes:
            return frame
        if target_minutes < source_minutes:
            logger.warning(
                "cannot upsample %s bars to %s for %s; returning source bars",
                source, target, symbol,
            )
            return frame

    idx = frame.index
    days = idx.normalize()
    if source_minutes:
        # Count source bars instead of wall-clock minutes: the lunch break is
        # one and a half hours of clock time but zero bars of trading time, so
        # a wall-clock bucket would split an afternoon session in two.
        per_bucket = max(int(round(target_minutes / source_minutes)), 1)
        ordinal = frame.groupby(days).cumcount()
        bucket = (ordinal // per_bucket).astype("int64")
        # Some feeds emit one extra bar per session (Tushare's 09:30 opening
        # auction is 49 bars a day instead of 48).  Left alone, that surplus
        # creates a fifth hourly bucket holding a single bar.  Clamping the
        # bucket index to the session's real capacity folds the surplus into the
        # last bucket instead, which is where its trading time belongs.
        per_session = frame.groupby(days).size()
        session_minutes = float(per_session.median()) * source_minutes
        capacity = max(int(round(session_minutes / target_minutes)), 1)
        bucket = bucket.clip(upper=capacity - 1)
    else:
        offset = pd.Series(idx - days, index=idx)
        anchor = offset.groupby(days).transform("min")
        elapsed = (offset - anchor).dt.total_seconds() / 60.0
        bucket = (elapsed // target_minutes).astype("int64")

    keys = pd.MultiIndex.from_arrays([days, bucket], names=["session", "bucket"])
    agg = {k: v for k, v in _OHLCV_AGG.items() if k in frame.columns}
    grouped = frame.groupby(keys, sort=True)
    out = grouped.agg(agg)

    stamps = grouped.apply(lambda part: part.index.max() if label == "end" else part.index.min())
    out.index = pd.DatetimeIndex(stamps.to_numpy(), name=frame.index.name or "trade_date")
    out = out.sort_index()
    return out[[c for c in ("open", "high", "low", "close", "volume") if c in out.columns]]


def clip_window(
    frame: pd.DataFrame, start_date: str, end_date: str, *, symbol: str = "",
    interval: str = "1D",
) -> pd.DataFrame:
    """Clip *frame* to the requested window and warn when coverage falls short.

    Public intraday endpoints serve a sliding window of recent bars (Tencent
    returns 320, Sina a few thousand), so a historical request silently yields a
    short frame.  A warning that names the actual coverage keeps that from
    looking like "the symbol had no trades".

    "Short" is measured in *missing sessions*, not wall-clock time: asking for
    a window that starts on a public holiday is fully covered even though the
    first bar lands a week after the requested start.
    """
    if frame is None or frame.empty:
        return frame
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    covered = frame.loc[(frame.index >= start) & (frame.index < end)]
    if covered.empty:
        return covered

    first = covered.index.min()
    missing = _sessions_before(start.date(), first.date())
    if missing is not None and missing > _MAX_MISSING_SESSIONS:
        logger.warning(
            "%s %s bars only cover %s .. %s (requested %s .. %s, %d sessions "
            "missing); the source serves a recent window",
            symbol, normalize_interval(interval), first, covered.index.max(),
            start_date, end_date, missing,
        )
    return covered


#: How many trading sessions may be absent before a short frame is reported.
#: Small gaps are normal (a suspension, a half-day, a holiday boundary).
_MAX_MISSING_SESSIONS = 3


def _sessions_before(start: object, first: object) -> int | None:
    """Count trading sessions strictly between *start* and *first*.

    Uses the project calendar when it is importable and falls back to business
    days otherwise, so this helper stays usable outside the agent package.
    Returns ``None`` when neither can be computed.
    """
    try:
        from src.data.calendar import trading_days

        span = trading_days(str(start), str(first))
        return len([day for day in span if day < str(first)])
    except Exception:  # noqa: BLE001 — calendar is optional here
        pass
    try:
        days = pd.bdate_range(str(start), str(first))
        return max(len(days) - 1, 0)
    except Exception:  # noqa: BLE001 — unparseable bounds
        return None
