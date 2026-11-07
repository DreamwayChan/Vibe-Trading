"""Tests for bar persistence in the lake and the annualisation defaults.

The lake is the only place a year of intraday bars can exist (the free feeds
serve a window), so these tests pin two properties that make repeated ingest
runs safe: upserts are idempotent, and reads filter by interval rather than
mixing 5-minute and hourly bars into one series.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data import bars as bar_store


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Point the lake at a temporary root for the duration of a test."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    return tmp_path


def _intraday_frame(day: str = "2024-01-02", *, step: str = "5min", base: float = 10.0):
    stamps = list(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq=step))
    stamps += list(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq=step))
    index = pd.DatetimeIndex(stamps)
    count = len(index)
    return pd.DataFrame(
        {
            "open": [base] * count,
            "high": [base + 1] * count,
            "low": [base - 1] * count,
            "close": [base + 0.5] * count,
            "volume": [100.0] * count,
        },
        index=index,
    )


def test_dataset_for_splits_daily_and_intraday():
    assert bar_store.dataset_for("1D") == bar_store.DATASET_DAILY
    assert bar_store.dataset_for("5m") == bar_store.DATASET_INTRADAY
    assert bar_store.dataset_for("1H") == bar_store.DATASET_INTRADAY


def test_save_and_load_roundtrip(lake):
    frame = _intraday_frame()
    written = bar_store.save_bars({"600519.SH": frame}, "5m", source="tencent")
    assert written == len(frame)

    loaded = bar_store.load_bars("5m", symbols=["600519.SH"])
    assert len(loaded) == len(frame)
    assert set(loaded["symbol"]) == {"600519.SH"}
    assert set(loaded["interval"]) == {"5m"}
    assert loaded["date"].min() == frame.index.min()

    by_symbol = bar_store.bars_by_symbol(loaded)
    assert list(by_symbol["600519.SH"].columns) == [
        "open", "high", "low", "close", "volume",
    ]


def test_repeated_ingest_is_idempotent(lake):
    frame = _intraday_frame()
    bar_store.save_bars({"600519.SH": frame}, "5m", source="tencent")
    first = bar_store.load_bars("5m")

    # A second run over the same sliding window must upsert, not append.
    bar_store.save_bars({"600519.SH": frame}, "5m", source="tencent")
    second = bar_store.load_bars("5m")

    assert len(second) == len(first)


def test_overlapping_windows_extend_history(lake):
    day_one = _intraday_frame("2024-01-02")
    day_two = _intraday_frame("2024-01-03")

    bar_store.save_bars({"600519.SH": day_one}, "5m")
    bar_store.save_bars({"600519.SH": day_two}, "5m")

    loaded = bar_store.load_bars("5m")
    assert len(loaded) == len(day_one) + len(day_two)


def test_intervals_are_isolated(lake):
    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m")
    bar_store.save_bars({"600519.SH": _intraday_frame(step="15min")}, "15m")

    assert len(bar_store.load_bars("5m")) == 48
    assert len(bar_store.load_bars("15m")) == 16


def test_load_filters_window(lake):
    bar_store.save_bars(
        {
            "600519.SH": _intraday_frame("2024-01-02"),
            "000001.SZ": _intraday_frame("2024-01-03"),
        },
        "5m",
    )

    only_second = bar_store.load_bars("5m", start="2024-01-03", end="2024-01-03")
    assert set(only_second["symbol"]) == {"000001.SZ"}

    one_symbol = bar_store.load_bars("5m", symbols=["600519.SH"])
    assert len(one_symbol) == 48


def test_coverage_reports_accumulated_span(lake):
    bar_store.save_bars({"600519.SH": _intraday_frame("2024-01-02")}, "5m")
    bar_store.save_bars({"600519.SH": _intraday_frame("2024-01-03")}, "5m")

    summary = bar_store.coverage("5m", symbols=["600519.SH"])
    assert len(summary) == 1
    row = summary.iloc[0]
    assert row["symbol"] == "600519.SH"
    assert row["bars"] == 96
    assert row["sessions"] == 2
    assert str(row["first"].date()) == "2024-01-02"
    assert str(row["last"].date()) == "2024-01-03"


def test_coverage_empty_lake(lake):
    summary = bar_store.coverage("5m")
    assert summary.empty
    assert list(summary.columns) == ["symbol", "bars", "first", "last", "sessions"]


def test_save_skips_non_datetime_index(lake):
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.Index([1]),
    )
    assert bar_store.save_bars({"X": frame}, "5m") == 0


def test_save_empty_input(lake):
    assert bar_store.save_bars({}, "5m") == 0


# ---------------------------------------------------------------------------
# Lake loader + annualisation
# ---------------------------------------------------------------------------


def test_lake_loader_reads_stored_bars(lake):
    from backtest.loaders.lake_loader import DataLoader as LakeLoader

    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m", source="tencent")

    loader = LakeLoader()
    assert loader.is_available() is True

    frames = loader.fetch(["600519.SH"], "2024-01-02", "2024-01-02", interval="5m")
    assert len(frames["600519.SH"]) == 48


def test_lake_loader_serves_daily_from_minutes(lake):
    from backtest.loaders.lake_loader import DataLoader as LakeLoader

    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m")
    frames = LakeLoader().fetch(["600519.SH"], "2024-01-02", "2024-01-02", interval="1D")
    # Nothing is stored at 1D, so it is aggregated from the 5m bars: one bar
    # for the single session.
    assert len(frames["600519.SH"]) == 1


def test_lake_loader_aggregates_finer_intervals(lake):
    from backtest.loaders.lake_loader import DataLoader as LakeLoader

    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m")

    frames = LakeLoader().fetch(["600519.SH"], "2024-01-02", "2024-01-02", interval="1H")
    hour = frames["600519.SH"]
    assert len(hour) == 4
    assert hour["volume"].sum() == 48 * 100.0


def test_lake_loader_empty_when_nothing_stored(lake):
    from backtest.loaders.lake_loader import DataLoader as LakeLoader

    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m")
    frames = LakeLoader().fetch(["999999.SZ"], "2024-01-02", "2024-01-02", interval="5m")
    assert frames == {}


def test_lake_loader_unavailable_on_empty_lake(lake):
    from backtest.loaders.lake_loader import DataLoader as LakeLoader

    assert LakeLoader().is_available() is False


def test_bars_per_year_defaults_to_a_share_session():
    from backtest.metrics import calc_bars_per_year

    # Sources nobody enumerated must not annualise at 1 bar/day.
    assert calc_bars_per_year("5m", "lake") == 252 * 48
    assert calc_bars_per_year("5m", "tencent") == 252 * 48
    assert calc_bars_per_year("1H", "lake") == 252 * 4
    assert calc_bars_per_year("1D", "lake") == 252
    # Enumerated sources keep their own session lengths.
    assert calc_bars_per_year("5m", "yfinance") == 252 * 78
    assert calc_bars_per_year("1m", "okx") == 365 * 1440


# ---------------------------------------------------------------------------
# Storage contract: partitioning and provenance
# ---------------------------------------------------------------------------


def test_intraday_is_partitioned_per_session(lake):
    """A session partition makes backfill O(new rows), not O(year)."""
    bar_store.save_bars(
        {"600519.SH": _intraday_frame("2024-01-02")}, "5m", source="tencent"
    )
    bar_store.save_bars(
        {"600519.SH": _intraday_frame("2024-01-03")}, "5m", source="tencent"
    )

    parts = sorted(p.parent.name for p in (lake / "bars_intraday").rglob("*.parquet"))
    assert parts == ["date=2024-01-02", "date=2024-01-03"]
    assert not (lake / "bars_intraday" / "year=2024").exists()


def test_daily_stays_partitioned_by_year(lake):
    frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
        },
        index=pd.DatetimeIndex(["2024-01-02"]),
    )
    bar_store.save_bars({"600519.SH": frame}, "1d", source="tushare")

    assert (lake / "bars_daily" / "year=2024" / "data.parquet").exists()


def test_bars_carry_provenance(lake):
    """``source`` and ``vintage`` are part of the contract, so they must reach disk."""
    bar_store.save_bars({"600519.SH": _intraday_frame()}, "5m", source="tencent")

    loaded = bar_store.load_bars("5m")
    assert set(loaded["source"]) == {"tencent"}
    assert loaded["vintage"].notna().all()


def test_writing_one_session_leaves_the_others_untouched(lake):
    """The point of per-session partitions: a backfill must not rewrite history."""
    bar_store.save_bars({"600519.SH": _intraday_frame("2024-01-02")}, "5m")
    first = lake / "bars_intraday" / "date=2024-01-02" / "data.parquet"
    stamp = first.stat().st_mtime_ns

    bar_store.save_bars({"600519.SH": _intraday_frame("2024-01-03")}, "5m")

    assert first.stat().st_mtime_ns == stamp
    assert len(bar_store.load_bars("5m")) == 96


def test_intraday_keeps_timestamp_precision(lake):
    """Partitioning by day must not truncate the bar timestamps."""
    frame = _intraday_frame()
    bar_store.save_bars({"600519.SH": frame}, "5m")

    loaded = bar_store.load_bars("5m")
    assert loaded["date"].max() == frame.index.max()
    assert loaded["date"].min() == frame.index.min()


def test_partition_file_read_keeps_timestamps(lake):
    """Reading a file under ``date=.../`` must not return the partition value.

    DuckDB treats a ``key=value`` path segment as a Hive partition and replaces
    the same-named column with it.  That turned every intraday bar into
    midnight and collapsed a session into one row on the next upsert — the
    merge path reads the existing partition through this function.
    """
    from src.data.lake import read_parquet_file

    frame = _intraday_frame()
    bar_store.save_bars({"600519.SH": frame}, "5m")

    path = lake / "bars_intraday" / "date=2024-01-02" / "data.parquet"
    stored = read_parquet_file(path)

    assert len(stored) == len(frame)
    assert stored["date"].max() == frame.index.max()
