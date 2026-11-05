"""Tests for the A-share trading calendar.

The calendar replaced a weekday-only span, so these tests pin the behaviours
that used to be wrong: exchange holidays, holiday-spanning shifts, and the
offline fallback that keeps a fully disconnected dev box working.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from src.data import calendar as cal


# 2024 exchange holidays this test cares about. Not the full list — each range
# is a real closure from the SSE/SZSE 2024 notice.
_HOLIDAYS_2024 = [
    (dt.date(2024, 1, 1), dt.date(2024, 1, 1)),      # New Year
    (dt.date(2024, 2, 9), dt.date(2024, 2, 17)),     # Spring Festival
    (dt.date(2024, 4, 4), dt.date(2024, 4, 6)),      # Qingming
    (dt.date(2024, 5, 1), dt.date(2024, 5, 5)),      # Labour Day
    (dt.date(2024, 6, 10), dt.date(2024, 6, 10)),    # Dragon Boat
    (dt.date(2024, 9, 15), dt.date(2024, 9, 17)),    # Mid-Autumn
    (dt.date(2024, 10, 1), dt.date(2024, 10, 7)),    # National Day
]


def _fake_sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    """Weekdays in the range minus the holiday closures."""
    closed: set[dt.date] = set()
    for lo, hi in _HOLIDAYS_2024:
        cur = lo
        while cur <= hi:
            closed.add(cur)
            cur += dt.timedelta(days=1)
    out: list[dt.date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur not in closed:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the lake at a temp dir and keep the calendar offline by default."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
    cal.clear_cache()
    yield tmp_path
    cal.clear_cache()


@pytest.fixture
def seeded(lake: Path, monkeypatch: pytest.MonkeyPatch):
    """Seed the calendar from a fake AKShare table instead of the network."""
    monkeypatch.delenv("VIBE_TRADING_CALENDAR_OFFLINE", raising=False)
    sessions = _fake_sessions(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: sessions)
    cal.refresh_calendar()
    cal._cache = None  # force a disk read on the next access
    yield sessions
    cal.clear_cache()


# ---------------------------------------------------------------------------
# Fallback path (no seed, no network)
# ---------------------------------------------------------------------------


def test_offline_fallback_skips_weekends(lake: Path):
    days = cal.trading_days("2024-01-01", "2024-01-07")
    assert days == [
        "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
    ]


def test_offline_fallback_still_treats_holiday_as_session(lake: Path):
    """Documents the old bug: without a seed, 2024-02-12 (春节) looks tradable."""
    assert cal.is_trading_day("2024-02-12") is True
    assert cal.calendar_source() in {"offline", "weekday-fallback"}


def test_empty_range_returns_empty(lake: Path):
    assert cal.trading_days("2024-03-05", "2024-03-04") == []


# ---------------------------------------------------------------------------
# Seeded calendar
# ---------------------------------------------------------------------------


def test_seed_excludes_spring_festival(seeded):
    assert cal.is_trading_day("2024-02-12") is False
    assert cal.is_trading_day("2024-02-08") is True
    assert cal.is_trading_day("2024-02-19") is True


def test_seed_excludes_national_day(seeded):
    assert cal.is_trading_day("2024-10-01") is False
    assert cal.is_trading_day("2024-09-30") is True
    assert cal.is_trading_day("2024-10-08") is True


def test_trading_days_span_matches_seed(seeded):
    days = cal.trading_days("2024-01-01", "2024-12-31")
    assert len(days) == len(seeded)
    assert days[0] == "2024-01-02"
    assert days[-1] == "2024-12-31"


def test_trading_day_count_is_holiday_aware(seeded):
    # 09-30 (Mon) plus 10-08..10-11: the whole National Day week is closed.
    assert cal.trading_day_count("2024-09-30", "2024-10-11") == 5

def test_count_excludes_closed_week(seeded):
    assert cal.trading_day_count("2024-10-01", "2024-10-07") == 0


def test_previous_trading_day_crosses_holiday(seeded):
    # First session after National Day is 2024-10-08; the day before it is 09-30.
    assert cal.previous_trading_day("2024-10-08") == "2024-09-30"
    assert cal.next_trading_day("2024-09-30") == "2024-10-08"


def test_shift_by_n_sessions(seeded):
    assert cal.previous_trading_day("2024-02-19", 1) == "2024-02-08"
    assert cal.next_trading_day("2024-02-08", 1) == "2024-02-19"
    assert cal.next_trading_day("2024-02-08", 0) == "2024-02-08"


def test_cache_roundtrip(seeded, lake: Path):
    cached = lake / "calendar" / "cn.parquet"
    assert cached.exists()

    from src.data.lake import read_parquet_file

    frame = read_parquet_file(cached)
    assert len(frame) == len(seeded)
    assert cal.calendar_source() in {"cache", "akshare"}


def test_partial_range_is_sliced(seeded):
    days = cal.trading_days("2024-02-01", "2024-02-08")
    assert days[0] == "2024-02-01"
    assert days[-1] == "2024-02-08"
    assert len(days) == 6


def test_range_after_coverage_tail_fills_with_weekdays(seeded):
    days = cal.trading_days("2025-01-01", "2025-01-05")
    # Beyond the seed, weekdays are used: Wed 1st .. Fri 3rd.
    assert days == ["2025-01-01", "2025-01-02", "2025-01-03"]


# ---------------------------------------------------------------------------
# Call-site behaviour (the helper capital flow requests are built on)
# ---------------------------------------------------------------------------


def test_span_helper_uses_real_calendar(seeded):
    from src.capital.sources import _trading_day_span

    assert len(_trading_day_span("2024-01-01", "2024-12-31")) == len(seeded)


def test_span_helper_returns_empty_for_closed_week(seeded):
    """A fully closed span is an answer, not a failure to fall back from.

    Regression: the helper used to treat an empty span as "calendar missing"
    and regenerate the holidays as weekdays.
    """
    from src.capital.sources import _trading_day_span

    assert _trading_day_span("2024-10-01", "2024-10-07") == []
    assert _trading_day_span("2024-02-09", "2024-02-17") == []


def test_span_helper_falls_back_when_unseeded(lake: Path):
    from src.capital.sources import _trading_day_span

    # No seed and offline: weekdays only, including the holiday.
    assert _trading_day_span("2024-10-01", "2024-10-07") == [
        "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    ]
