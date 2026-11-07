"""Catalog tests: a manifest must describe the lake, not an aspiration.

The console's whole value rests on these numbers being derived from what is on
disk, so each test writes real parquet and then checks the scan against it —
including the negative case where a write bypassed the upsert and produced
duplicate keys.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.data import bars as bar_store
from src.data import catalog
from src.data.registry import spec


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Isolate the lake for one test."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
    return tmp_path


def _frames(*days: str) -> dict[str, pd.DataFrame]:
    stamps: list[pd.Timestamp] = []
    for day in days:
        stamps.extend(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="30min"))
        stamps.extend(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="30min"))
    index = pd.DatetimeIndex(stamps)
    count = len(index)
    frame = pd.DataFrame(
        {
            "open": [10.0] * count,
            "high": [10.5] * count,
            "low": [9.8] * count,
            "close": [10.2] * count,
            "volume": [1000.0] * count,
        },
        index=index,
    )
    return frame


def test_rebuild_on_empty_lake_writes_an_empty_manifest(lake):
    payload = catalog.rebuild("bars_daily")

    assert payload["rows"] == 0
    assert payload["files"] == 0
    assert payload["partitions"] == []
    assert catalog.manifest_path("bars_daily").is_file()


def test_rebuild_counts_rows_symbols_and_span(lake):
    bar_store.save_bars({"600519.SH": _frames("2024-01-02", "2024-01-03")}, "30m")
    bar_store.save_bars({"000001.SZ": _frames("2024-01-02")}, "30m")

    payload = catalog.rebuild("bars_intraday")

    assert payload["rows"] == 24
    assert payload["symbols"] == 2
    assert payload["date_min"] == "2024-01-02"
    assert payload["date_max"] == "2024-01-03"
    assert payload["quality"]["dup_keys"] == 0
    # Intraday is stored per session, so the partition name is the date.
    partition_names = [row["name"] for row in payload["partitions"]]
    assert partition_names == ["date=2024-01-02", "date=2024-01-03"]


def test_rebuild_reports_duplicate_keys(lake):
    """A write that skips the upsert must be visible as a data defect."""
    import duckdb

    target = lake / "bars_intraday" / "year=2024"
    target.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "symbol": ["600519.SH", "600519.SH"],
            "date": pd.to_datetime(["2024-01-02 09:35", "2024-01-02 09:35"]),
            "interval": ["30m", "30m"],
            "open": [10.0, 10.0],
            "high": [10.5, 10.5],
            "low": [9.8, 9.8],
            "close": [10.2, 10.2],
            "volume": [100.0, 100.0],
        }
    )
    con = duckdb.connect()
    try:
        con.register("frame", frame)
        con.execute(f"COPY frame TO '{(target / 'data.parquet').as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()

    payload = catalog.rebuild("bars_intraday")

    assert payload["rows"] == 2
    assert payload["quality"]["dup_keys"] == 1


def test_declared_quality_predicates_run(lake):
    """`low > open` is impossible; the declared check must catch it."""
    import duckdb

    target = lake / "bars_intraday" / "year=2024"
    target.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": pd.to_datetime(["2024-01-02 09:35"]),
            "interval": ["30m"],
            "open": [10.0],
            "high": [10.5],
            "low": [10.4],
            "close": [10.2],
            "volume": [100.0],
        }
    )
    con = duckdb.connect()
    try:
        con.register("frame", frame)
        con.execute(f"COPY frame TO '{(target / 'data.parquet').as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()

    payload = catalog.rebuild("bars_intraday")
    checks = {c["id"]: c["violations"] for c in payload["quality"]["checks"]}

    assert checks["ohlc_order"] == 1
    assert checks["non_positive_price"] == 0


def test_schema_drift_flags_missing_columns(lake):
    bar_store.save_bars({"600519.SH": _frames("2024-01-02")}, "30m")

    payload = catalog.rebuild("bars_intraday")

    assert "source" in payload["schema_drift"]["missing"]
    assert "year" not in payload["schema_drift"]["extra"]


def test_touch_never_fakes_a_row_count(lake):
    bar_store.save_bars({"600519.SH": _frames("2024-01-02")}, "30m")
    catalog.rebuild("bars_intraday")

    updated = catalog.touch("bars_intraday", rows=12, source="tencent", job="job_test")

    assert updated is not None
    assert updated["last_source"] == "tencent"
    assert updated["last_job"] == "job_test"
    assert updated["dirty"] is True
    # The scanned count stays authoritative; the increment only feeds an estimate
    # until the next rebuild overwrites it.
    assert updated["rows"] == 8
    assert updated["rows_estimate"] == 20


def test_touch_without_data_and_without_manifest_is_a_noop(lake):
    assert catalog.touch("bars_daily", rows=5) is None


def test_rebuild_all_skips_unknown_names(lake):
    result = catalog.rebuild_all(["bars_daily", "does_not_exist"])

    assert "bars_daily" in result
    assert "does_not_exist" not in result


def test_manifest_is_valid_json_on_disk(lake):
    bar_store.save_bars({"600519.SH": _frames("2024-01-02")}, "30m")
    catalog.rebuild("bars_intraday")

    payload = json.loads(catalog.manifest_path("bars_intraday").read_text(encoding="utf-8"))

    assert payload["dataset"] == "bars_intraday"
    assert payload["schema_version"] == catalog.SCHEMA_VERSION


def test_spec_declarations_are_self_consistent():
    """Every spec must be addressable and its key columns must exist."""
    for dataset_spec in (spec(n) for n in ("bars_daily", "bars_intraday", "tick_agg", "tradability", "calendar")):
        assert dataset_spec.name
        assert set(dataset_spec.keys) <= set(dataset_spec.column_names) or dataset_spec.name == "calendar"
        assert dataset_spec.retention_days is None or dataset_spec.retention_days > 0
