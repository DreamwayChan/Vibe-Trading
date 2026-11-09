"""Calendar edges: cache staleness, seed failures, and the disk-only lookback.

``test_trading_calendar.py`` pins the holiday behaviour on a healthy seed. The
failures that hurt in production are the other ones: a corrupt seed file, an
AKShare outage, a seed that stops months before "today", and the code path a
daily refresh takes when it must not touch the network. Those decide whether a
backtest silently starts counting holidays as sessions.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from src.data import calendar as cal


def _forget() -> None:
    """Drop the in-process calendar without touching the on-disk seed."""
    cal._cache = None
    cal._cache_source = "unknown"
    cal._warned_fallback = False
    cal._last_refresh_attempt = 0.0


@pytest.fixture(autouse=True)
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path / "lake"))
    monkeypatch.delenv("VIBE_TRADING_CALENDAR_OFFLINE", raising=False)
    _forget()
    yield tmp_path / "lake"
    _forget()


def _seed(dates: list[dt.date]) -> None:
    cal._write_cache(dates, "akshare")


def _weekdays(start: dt.date, end: dt.date) -> list[str]:
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


# Real SSE/SZSE 2024 closures. A weekday-only fake seed would make the holiday
# assertions below pass for the wrong reason.
_HOLIDAYS_2024 = [
    (dt.date(2024, 1, 1), dt.date(2024, 1, 1)),
    (dt.date(2024, 2, 9), dt.date(2024, 2, 17)),
    (dt.date(2024, 4, 4), dt.date(2024, 4, 6)),
    (dt.date(2024, 5, 1), dt.date(2024, 5, 5)),
    (dt.date(2024, 6, 10), dt.date(2024, 6, 10)),
    (dt.date(2024, 9, 15), dt.date(2024, 9, 17)),
    (dt.date(2024, 10, 1), dt.date(2024, 10, 7)),
]


def _sessions(start: dt.date, end: dt.date) -> list[dt.date]:
    closed: set[dt.date] = set()
    for lo, hi in _HOLIDAYS_2024:
        cur = lo
        while cur <= hi:
            closed.add(cur)
            cur += dt.timedelta(days=1)
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5 and cur not in closed:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def _recent_sessions(count: int = 5) -> list[dt.date]:
    """Sessions ending a couple of days ago, so the seed is not stale."""
    today = dt.date.today()
    out: list[dt.date] = []
    cur = today - dt.timedelta(days=2)
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return sorted(out)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_recent_seed_is_served_from_cache_without_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed that reaches today must not trigger a network call per lookup."""

    def _boom() -> list[dt.date]:
        raise AssertionError("refresh must not run for a fresh seed")

    _seed(_recent_sessions())
    monkeypatch.setattr(cal, "_fetch_from_akshare", _boom)

    days = cal.trading_days(_recent_sessions()[0].isoformat(), dt.date.today().isoformat())

    assert days
    assert cal.calendar_source() == "cache"


def test_seed_ending_inside_the_slack_window_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exchange publishes holidays late; a seed days behind is fine."""
    last = dt.date.today() - dt.timedelta(days=cal._STALE_AFTER_DAYS - 1)
    dates = [last - dt.timedelta(days=7), last]
    assert cal._is_stale(dates) is False

    _seed(dates)
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: pytest.fail("unexpected refresh"))
    assert cal.calendar_dates() == dates


def test_seed_older_than_the_slack_window_is_stale() -> None:
    last = dt.date.today() - dt.timedelta(days=cal._STALE_AFTER_DAYS + 1)
    assert cal._is_stale([last]) is True
    assert cal._is_stale([]) is True


def test_stale_seed_is_refreshed_and_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = [dt.date(2019, 1, 2), dt.date(2019, 1, 3)]
    _seed(stale)
    fresh = _recent_sessions()
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: fresh)

    assert cal.calendar_dates() == fresh
    assert cal.calendar_source() == "akshare"
    # The new seed is on disk too, so a restart does not re-fetch.
    _forget()
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: pytest.fail("cache should be warm"))
    assert cal.calendar_dates() == fresh
    assert cal.calendar_source() == "cache"


def test_stale_seed_still_refreshes_in_the_first_minutes_after_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_last_refresh_attempt == 0`` means never tried, not tried at boot.

    ``time.monotonic()`` is seconds since boot. Treating 0 as a real attempt
    skipped the refresh whenever the machine had been up for less than the
    backoff window, so a fresh runner failed the offline suite.
    """
    stale = [dt.date(2019, 1, 2), dt.date(2019, 1, 3)]
    _seed(stale)
    fresh = _recent_sessions()
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: fresh)
    monkeypatch.setattr(cal.time, "monotonic", lambda: 30.0)
    cal._last_refresh_attempt = 0.0

    assert cal.calendar_dates() == fresh
    assert cal.calendar_source() == "akshare"


# ---------------------------------------------------------------------------
# Corrupt / foreign cache files
# ---------------------------------------------------------------------------


def test_corrupt_cache_is_ignored_and_reseeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = cal._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file at all")
    fresh = _recent_sessions()
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: fresh)

    with caplog.at_level(logging.WARNING, logger=cal.__name__):
        days = cal.calendar_dates()

    assert days == fresh
    assert any("unreadable" in record.message for record in caplog.records)
    assert cal.calendar_source() == "akshare"


def test_cache_without_a_trade_date_column_is_discarded() -> None:
    from src.data.lake import write_parquet

    path = cal._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_parquet(pd.DataFrame({"trading_day": ["2024-01-02"]}), path)

    assert cal._read_cache() == []


# ---------------------------------------------------------------------------
# Seeding from AKShare
# ---------------------------------------------------------------------------


def _fake_akshare(frame: pd.DataFrame) -> types.ModuleType:
    module = types.ModuleType("akshare")
    module.tool_trade_date_hist_sina = lambda: frame  # type: ignore[attr-defined]
    return module


def test_seed_reader_sorts_dedupes_and_drops_pre_1990_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {
            "calendar_date": [
                "2024-01-03",
                "1989-12-29",
                "2024-01-02",
                "2024-01-03",
                None,
                "not-a-date",
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(frame))

    assert cal._fetch_from_akshare() == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]


def test_seed_reader_prefers_the_trade_date_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"trade_date": ["2024-02-09"], "calendar_date": ["1999-01-01"]}
    )
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(frame))

    assert cal._fetch_from_akshare() == [dt.date(2024, 2, 9)]


def test_seed_reader_rejects_an_empty_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty table must raise, not seed an empty calendar that reads as 'no sessions'."""
    frame = pd.DataFrame({"trade_date": [None, None]})
    monkeypatch.setitem(sys.modules, "akshare", _fake_akshare(frame))

    with pytest.raises(RuntimeError, match="empty trade-date"):
        cal._fetch_from_akshare()


# ---------------------------------------------------------------------------
# Network outage
# ---------------------------------------------------------------------------


def test_outage_falls_back_to_weekdays_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead seed degrades to the old weekday span — loudly, and only once."""

    def _offline() -> list[dt.date]:
        raise OSError("akshare unreachable")

    monkeypatch.setattr(cal, "_fetch_from_akshare", _offline)

    with caplog.at_level(logging.WARNING, logger=cal.__name__):
        first = cal.trading_days("2024-10-01", "2024-10-07")
        second = cal.trading_days("2024-10-01", "2024-10-07")

    assert first == second == _weekdays(dt.date(2024, 10, 1), dt.date(2024, 10, 7))
    assert cal.calendar_source() == "weekday-fallback"
    warnings = [r for r in caplog.records if "falling back to weekday spans" in r.message]
    assert len(warnings) == 1, "the outage must be reported once, not per lookup"


def test_offline_flag_never_writes_a_weekday_seed(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback must stay in memory: a poisoned cache would outlive the outage."""
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")

    assert cal.trading_days("2024-01-01", "2024-01-03")
    assert cal.calendar_source() == "offline"
    assert not (lake / "calendar" / "cn.parquet").exists()


def test_clear_cache_survives_a_locked_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows keeps a handle open on a file another process is reading."""

    class _Locked:
        def exists(self) -> bool:
            return True

        def unlink(self) -> None:
            raise OSError("file in use")

    monkeypatch.setattr(cal, "_cache_path", lambda: _Locked())

    cal.clear_cache()  # must not raise

    assert cal._cache is None
    assert cal._cache_source == "unknown"


# ---------------------------------------------------------------------------
# Range coverage: head, tail, outside
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(monkeypatch: pytest.MonkeyPatch) -> list[dt.date]:
    sessions = _sessions(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: sessions)
    cal.refresh_calendar()
    _forget()
    return sessions


def test_head_gap_wider_than_a_holiday_is_filled_with_weekdays(seeded: list[dt.date]) -> None:
    """A seed that starts later than the request is a coverage gap, not a closure."""
    days = cal.trading_days("2023-11-01", "2024-01-03")

    assert days[0] == "2023-11-01"
    assert "2024-01-01" in days  # approximated, and that is the documented cost
    assert days[-1] == "2024-01-03"


def test_short_lead_in_before_the_seed_stays_closed(seeded: list[dt.date]) -> None:
    """2024-01-01 is a one-day lead-in, i.e. a real holiday, so it must stay out."""
    days = cal.trading_days("2024-01-01", "2024-01-03")

    assert days == ["2024-01-02", "2024-01-03"]


def test_dates_before_or_after_coverage_fall_back_to_weekdays(seeded: list[dt.date]) -> None:
    assert cal.is_trading_day("2019-06-03") is True  # Monday
    assert cal.is_trading_day("2019-06-01") is False  # Saturday
    assert cal.is_trading_day("2030-01-02") is True
    assert cal.is_trading_day(dt.date(2024, 2, 12)) is False  # inside the seed: Spring Festival


def test_shift_without_a_seed_steps_over_weekends(
    lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline, a shift still has to land on a plausible session."""
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")

    assert cal.next_trading_day("2024-06-07") == "2024-06-10"  # Fri -> Mon
    assert cal.previous_trading_day("2024-06-10") == "2024-06-07"
    assert cal.next_trading_day("2024-06-07", 2) == "2024-06-11"
    assert cal.next_trading_day("2024-06-08", 0) == "2024-06-08"


def test_shift_accepts_datetime_and_date_inputs(seeded: list[dt.date]) -> None:
    assert cal.next_trading_day(dt.datetime(2024, 2, 8, 15, 0)) == "2024-02-19"
    assert cal.previous_trading_day(dt.date(2024, 2, 19)) == "2024-02-08"
    assert cal.next_trading_day("2024-02-08") == "2024-02-19"


# ---------------------------------------------------------------------------
# Disk-only lookback (what the daily top-up uses)
# ---------------------------------------------------------------------------


def test_cached_previous_day_reads_disk_without_a_network_call(
    seeded: list[dt.date], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: pytest.fail("must not fetch"))
    assert cal._cache is None

    # Within the staleness window of the seed, so the disk copy is authoritative.
    assert cal.cached_previous_trading_day("2025-01-02") == "2024-12-31"


def test_cached_previous_day_zero_steps_returns_the_day_itself(seeded: list[dt.date]) -> None:
    assert cal.cached_previous_trading_day("2024-10-08", 0) == "2024-10-08"


def test_cached_previous_day_is_none_without_a_seed(lake: Path) -> None:
    assert cal.cached_previous_trading_day("2024-10-08") is None


def test_cached_previous_day_refuses_a_stale_seed(seeded: list[dt.date]) -> None:
    """A seed that stopped last year must not be read as 'the market stopped'."""
    assert cal.cached_previous_trading_day(dt.date.today(), 1) is None


def test_cached_previous_day_needs_enough_history(seeded: list[dt.date]) -> None:
    # The seed starts 2024-01-02: only one prior session exists on 2024-01-03.
    assert cal.cached_previous_trading_day("2024-01-03", 1) == "2024-01-02"
    assert cal.cached_previous_trading_day("2024-01-03", 5) is None


def test_cached_previous_day_uses_the_memory_copy_when_warm(seeded: list[dt.date]) -> None:
    cal.calendar_dates()  # warm the process cache
    assert cal._cache is not None
    assert cal.cached_previous_trading_day("2024-02-19") == "2024-02-08"


# ---------------------------------------------------------------------------
# Ingest wrapper
# ---------------------------------------------------------------------------


def test_ingest_refresh_reports_what_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.data.ingest import calendar as ingest_calendar

    sessions = [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
    monkeypatch.setattr(cal, "refresh_calendar", lambda: len(sessions))
    monkeypatch.setattr(cal, "calendar_dates", lambda: sessions)

    report = ingest_calendar.refresh()

    assert report.dataset == "calendar"
    assert report.rows == report.sessions == 2
    assert (report.start, report.end) == ("2024-01-02", "2024-01-03")
