"""Tests for the lake layout migration.

The migration exists because the writer changed: ``bars_intraday`` used to be
stored per year and is now stored per session.  A dataset that exists in both
layouts at once would double count, so these tests pin the three properties that
make the rewrite safe — row preservation, layout normalisation, and a refusal to
touch data it cannot read.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.data import catalog, lake, migrate
from src.data.registry import spec


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Point the lake at a temporary root and reset the catalog with it."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    return tmp_path


def _old_layout_frame():
    """A frame shaped like the pre-migration writer produced: with a ``year``
    helper column and a timestamp ``date``."""
    stamps = [f"2024-01-02 09:{m:02d}" for m in (30, 35, 40)]
    frame = pd.DataFrame(
        {
            "symbol": ["600519.SH"] * 3,
            "date": pd.to_datetime(stamps),
            "interval": ["5m"] * 3,
            "open": [10.0, 10.1, 10.2],
            "high": [10.5, 10.6, 10.7],
            "low": [9.9, 10.0, 10.1],
            "close": [10.4, 10.5, 10.6],
            "volume": [100.0, 200.0, 300.0],
        }
    )
    return frame


def test_current_partitions_reads_the_path(root):
    lake.write_dataset(
        _old_layout_frame(), "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )

    assert migrate.current_partitions("bars_intraday") == ("year",)
    assert migrate.layout_mismatch("bars_intraday") == (("year",), ("date",))


def test_migrate_rewrites_to_declared_layout(root):
    frame = _old_layout_frame()
    lake.write_dataset(
        frame, "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )

    report = migrate.migrate_dataset("bars_intraday")

    assert report["status"] == "ok"
    assert report["rows"] == len(frame)
    assert report["from"] == ["year"]
    assert report["to"] == ["date"]
    on_disk = sorted(p.parent.name for p in (root / "bars_intraday").rglob("*.parquet"))
    assert on_disk == ["date=2024-01-02"]
    assert migrate.layout_mismatch("bars_intraday") is None


def test_migration_preserves_timestamps(root):
    """A layout rewrite must not round-trip through the day."""
    frame = _old_layout_frame()
    lake.write_dataset(
        frame, "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )

    migrate.migrate_dataset("bars_intraday")

    stored = lake.read_dataset("bars_intraday")
    assert list(stored["date"]) == list(frame["date"])


def test_migration_is_idempotent(root):
    frame = _old_layout_frame()
    lake.write_dataset(
        frame, "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )
    migrate.migrate_dataset("bars_intraday")

    second = migrate.migrate_dataset("bars_intraday")

    assert second["status"] == "skipped"
    assert len(lake.read_dataset("bars_intraday")) == len(frame)


def test_migration_drops_the_old_partition_helper_column(root):
    frame = _old_layout_frame()
    frame["year"] = 2024  # what the old writer injected
    lake.write_dataset(
        frame, "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )

    migrate.migrate_dataset("bars_intraday")

    stored = lake.read_dataset("bars_intraday")
    assert "year" not in stored.columns


def test_migration_backfills_provenance(root):
    """Rows written before ``source``/``vintage`` existed get honest defaults."""
    lake.write_dataset(
        _old_layout_frame(), "bars_intraday", partition_by="year", key=("symbol", "interval", "date")
    )

    migrate.migrate_dataset("bars_intraday")

    stored = lake.read_dataset("bars_intraday")
    assert set(stored["source"]) == {"unknown"}
    assert stored["vintage"].notna().all()
    manifest = catalog.rebuild("bars_intraday")
    # Migration backfills provenance, not measurements. The pre-migration writer
    # never recorded `amount`, so it stays in the drift report (honest gap that
    # points at a re-ingest) instead of being invented as a null column.
    missing = manifest["schema_drift"]["missing"]
    assert missing == ["amount"], missing


def test_rewrite_forces_normalisation_when_layout_already_matches(root):
    frame = _old_layout_frame()
    lake.write_dataset(
        frame, "bars_intraday", partition_by="date", key=("symbol", "interval", "date")
    )
    assert migrate.migrate_dataset("bars_intraday")["status"] == "skipped"

    report = migrate.migrate_dataset("bars_intraday", rewrite=True)

    assert report["status"] == "ok"
    assert set(lake.read_dataset("bars_intraday")["source"]) == {"unknown"}


def test_unreadable_dataset_is_left_alone(root):
    """An unreadable partition must not be "migrated" into nothing."""
    target = root / "bars_intraday" / "year=2024"
    target.mkdir(parents=True)
    (target / "data.parquet").write_bytes(b"not a parquet file")

    with pytest.raises(Exception):
        migrate.migrate_dataset("bars_intraday")

    assert (target / "data.parquet").exists()


def test_declared_partition_is_used_by_the_writer(root):
    """The migration only mirrors the writer; both must read the same contract."""
    assert spec("bars_intraday").partitions == ("date",)
    assert spec("bars_daily").partitions == ("year",)
