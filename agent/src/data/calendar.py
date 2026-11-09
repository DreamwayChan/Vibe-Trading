"""A-share trading calendar backed by the lake, seeded from AKShare.

Why this module exists
----------------------
``src/capital/sources.py`` used to derive "trading days" by skipping weekends::

    if cur.weekday() < 5:      # Spring Festival is a weekday too

That treated every exchange holiday as a session.  A-share calendars lose 10-15
sessions a year to holidays, so every fund-flow window, every ``window=N``
lookback and every annualisation built on the old helper was off by up to ~6%.
The damage was silent: the spans were still "self-consistent", just wrong.

Design
------
* The authoritative list is seeded once from
  ``akshare.tool_trade_date_hist_sina()`` and cached under ``<lake>/calendar``.
* The weekday fallback survives for offline/test environments, but it is never
  written to the cache — a poisoned cache would outlive the outage.
* Nothing hits the network at import time.  The first call that needs calendar
  data loads the cache, and refreshes it only when it is stale or incomplete.
* All public helpers take and return ``YYYY-MM-DD`` strings, matching the old
  ``_trading_day_span`` contract so call sites do not change shape.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Iterable, Sequence

import pandas as pd

from src.config.accessor import read_env

logger = logging.getLogger(__name__)

__all__ = [
    "calendar_dates",
    "calendar_source",
    "has_calendar",
    "trading_days",
    "is_trading_day",
    "next_trading_day",
    "previous_trading_day",
    "cached_previous_trading_day",
    "trading_day_count",
    "refresh_calendar",
    "clear_cache",
]

DATASET = "calendar"
_FILENAME = "cn.parquet"
# The exchange publishes next-year holidays in December; a week of slack keeps a
# long-running process from serving a calendar that ends before "today".
_STALE_AFTER_DAYS = 10
_EARLIEST_YEAR = 1990
_OFFLINE_ENV = "VIBE_TRADING_CALENDAR_OFFLINE"
# Only fill a head gap this large: a request starting a day or two before the
# first session is just a leading holiday (2024-01-01), and filling it with
# weekdays would fabricate a session. A gap this wide means the seed simply
# starts later than the request (e.g. seeded from 2015, backtesting 2010).
_HEAD_FILL_MIN_DAYS = 31

_lock = threading.RLock()
_cache: list[dt.date] | None = None
_cache_source: str = "unknown"
_warned_fallback = False
_last_refresh_attempt: float = 0.0
# A stale seed should not trigger a network call on every lookup: capital flow
# loops call trading_days() hundreds of times per scan.
_REFRESH_BACKOFF_S = 600.0


# ---------------------------------------------------------------------------
# Cache plumbing
# ---------------------------------------------------------------------------


def _cache_path():
    from src.data.lake import lake_root

    return lake_root() / DATASET / _FILENAME


def _offline() -> bool:
    """True when network access for the calendar is disabled."""
    return read_env(_OFFLINE_ENV).strip().lower() in {"1", "true", "yes", "on"}


def clear_cache() -> None:
    """Drop the in-process calendar and delete the on-disk seed."""
    global _cache, _cache_source
    with _lock:
        _cache = None
        _cache_source = "unknown"
        path = _cache_path()
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:  # noqa: BLE001 — best effort
                logger.warning("could not remove calendar cache %s: %s", path, exc)


def _read_cache() -> list[dt.date]:
    path = _cache_path()
    if not path.exists():
        return []
    try:
        from src.data.lake import read_parquet_file

        frame = read_parquet_file(path)
    except Exception as exc:  # noqa: BLE001 — corrupt cache is re-seeded
        logger.warning("calendar cache unreadable (%s); re-seeding", exc)
        return []
    if "trade_date" not in frame.columns:
        return []
    return [ts.date() for ts in pd.to_datetime(frame["trade_date"]).dropna()]


def _write_cache(dates: Sequence[dt.date], source: str) -> None:
    from src.data.lake import write_parquet

    path = _cache_path()
    frame = pd.DataFrame(
        {"trade_date": pd.to_datetime(list(dates)), "source": source}
    ).drop_duplicates(subset=["trade_date"], keep="last")
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    tmp = path.with_suffix(".parquet.tmp")
    write_parquet(frame, tmp)
    os.replace(tmp, path)


def _is_stale(dates: Sequence[dt.date]) -> bool:
    """True when the cached calendar cannot answer queries up to today."""
    if not dates:
        return True
    today = dt.date.today()
    if dates[-1] >= today - dt.timedelta(days=_STALE_AFTER_DAYS):
        return False
    # A calendar that ends before today is only "stale" if it plausibly should
    # have grown: only refresh on or after the first weekday past the horizon.
    return True


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def _fetch_from_akshare() -> list[dt.date]:
    """Seed the calendar from AKShare's Sina trade-date table."""
    import akshare as ak

    frame = ak.tool_trade_date_hist_sina()
    column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
    stamps = pd.to_datetime(frame[column], errors="coerce").dropna()
    dates = sorted({ts.date() for ts in stamps if ts.year >= _EARLIEST_YEAR})
    if not dates:
        raise RuntimeError("akshare returned an empty trade-date table")
    return dates


def refresh_calendar() -> int:
    """Force a re-seed from the network.  Returns the number of sessions."""
    global _cache, _cache_source
    with _lock:
        dates = _fetch_from_akshare()
        _write_cache(dates, "akshare")
        _cache = dates
        _cache_source = "akshare"
        logger.info("trading calendar refreshed: %d sessions through %s", len(dates), dates[-1])
        return len(dates)


def _weekday_span(start: dt.date, end: dt.date) -> list[dt.date]:
    """Old behaviour, kept as the offline fallback: skip weekends only."""
    out: list[dt.date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _ensure_loaded() -> list[dt.date]:
    """Load the calendar into memory, seeding it when missing or stale."""
    global _cache, _cache_source, _warned_fallback, _last_refresh_attempt

    with _lock:
        if _cache is None:
            cached = _read_cache()
            if cached:
                _cache = cached
                _cache_source = "cache"

        if _cache is not None and not _is_stale(_cache):
            return _cache

        if _offline():
            if _cache is None:
                _cache = []
                _cache_source = "offline"
            return _cache

        # Back off after a failed or recent attempt, but only while there is
        # something to serve; a cold start must still try.
        now = time.monotonic()
        if _cache and (now - _last_refresh_attempt) < _REFRESH_BACKOFF_S:
            return _cache

        _last_refresh_attempt = now
        try:
            return refresh_calendar_and_get()
        except Exception as exc:  # noqa: BLE001 — degrade, never crash callers
            if not _warned_fallback:
                _warned_fallback = True
                logger.warning(
                    "trading calendar unavailable (%s); falling back to weekday spans — "
                    "holidays will be treated as sessions until the seed succeeds",
                    exc,
                )
            if _cache is None:
                _cache = []
                _cache_source = "weekday-fallback"
            return _cache


def refresh_calendar_and_get() -> list[dt.date]:
    """Refresh from the network and return the loaded calendar."""
    refresh_calendar()
    return _cache or []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calendar_dates() -> list[dt.date]:
    """Return every known session date, ascending."""
    return list(_ensure_loaded())


def calendar_source() -> str:
    """Return where the current calendar came from (cache/akshare/fallback)."""
    _ensure_loaded()
    return _cache_source


def has_calendar() -> bool:
    """True when a real (seeded or cached) calendar is loaded.

    Callers that fall back to a weekday approximation must branch on this, not
    on ``trading_days(...)`` being empty: a fully closed span (National Day) is
    a legitimate empty answer, and treating it as "calendar unavailable" put
    the holiday right back into the span.
    """
    return bool(_ensure_loaded())


def _to_date(value: str | dt.date | dt.datetime | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


def trading_days(start, end) -> list[str]:
    """Return the session dates in ``[start, end]`` as ``YYYY-MM-DD`` strings.

    Dates outside the seeded calendar's coverage are filled with weekdays and
    logged at debug level: a backtest that ends "tomorrow" should not return an
    empty date index, but the caller deserves to know the tail is approximate.
    """
    start_d = _to_date(start)
    end_d = _to_date(end)
    if end_d < start_d:
        return []

    dates = _ensure_loaded()
    if not dates:
        return [d.isoformat() for d in _weekday_span(start_d, end_d)]

    first, last = dates[0], dates[-1]
    covered = [d for d in dates if start_d <= d <= end_d]

    if start_d < first:
        gap_days = (first - start_d).days
        if gap_days > _HEAD_FILL_MIN_DAYS:
            head = _weekday_span(start_d, first - dt.timedelta(days=1))
            covered = head + covered
            logger.debug(
                "trading_days: %s precedes calendar coverage (%s); head filled with weekdays",
                start_d, first,
            )
        else:
            logger.debug(
                "trading_days: %s..%s precedes the first seeded session (%s); "
                "treated as a closed lead-in",
                start_d, first, first,
            )
    if end_d > last:
        tail = _weekday_span(last + dt.timedelta(days=1), end_d)
        covered = covered + tail
        logger.warning(
            "trading_days: %s exceeds calendar coverage (%s); tail filled with weekdays "
            "(refresh the calendar seed to remove this approximation)",
            end_d, last,
        )
    return [d.isoformat() for d in covered]


def is_trading_day(day) -> bool:
    """True when ``day`` is an exchange session."""
    target = _to_date(day)
    dates = _ensure_loaded()
    if not dates:
        return target.weekday() < 5
    if target < dates[0] or target > dates[-1]:
        return target.weekday() < 5
    return target in set(dates)


def trading_day_count(start, end) -> int:
    """Number of sessions in ``[start, end]``."""
    return len(trading_days(start, end))


def next_trading_day(day, n: int = 1) -> str:
    """Return the session ``n`` steps after ``day`` (``n`` may be 0)."""
    return _shift(day, abs(int(n)), forward=True)


def previous_trading_day(day, n: int = 1) -> str:
    """Return the session ``n`` steps before ``day``."""
    return _shift(day, abs(int(n)), forward=False)


def cached_previous_trading_day(day, n: int = 1) -> str | None:
    """Previous session from the seed already on disk or in memory.

    Does not contact the network and does not invent weekdays. Returns None
    when no seed is loaded, or when the seed ends more than
    ``_STALE_AFTER_DAYS`` before ``day`` — a stale seed must not be treated as
    "the market stopped", or a daily refresh would freeze at the seed's last
    session instead of topping up through the real T-1.
    """
    target = _to_date(day)
    steps = abs(int(n))
    if steps == 0:
        return target.isoformat()
    dates = _cache
    if not dates:
        try:
            dates = _read_cache()
        except Exception:  # noqa: BLE001 — no seed, caller uses a weekday fallback
            return None
    if not dates:
        return None
    if dates[-1] < target - dt.timedelta(days=_STALE_AFTER_DAYS):
        return None
    prior = [item for item in dates if item < target]
    if len(prior) < steps:
        return None
    return prior[-steps].isoformat()


def _shift(day, n: int, *, forward: bool) -> str:
    target = _to_date(day)
    if n == 0:
        return target.isoformat()
    dates = set(_ensure_loaded())
    if not dates:
        step = 1 if forward else -1
        cur = target
        moved = 0
        while moved < n:
            cur += dt.timedelta(days=step)
            if cur.weekday() < 5:
                moved += 1
        return cur.isoformat()

    cur = target
    moved = 0
    for _ in range(400):  # bounded: never loop forever on a sparse calendar
        cur += dt.timedelta(days=1 if forward else -1)
        if cur in dates:
            moved += 1
            if moved >= n:
                break
    return cur.isoformat()
