"""One-shot layout migrations for the lake.

Partitioning is declared in the registry, but a dataset written before that
declaration can disagree with it — ``bars_intraday`` was stored as
``year=2024/data.parquet`` while the spec says ``date=``.  Reading both layouts
at once would double-count rows, so the fix has to rewrite the dataset, not
just change the writer.

The rewrite is crash-safe in the sense that matters: the new layout is written
to a sibling directory first and only swapped in after it has the expected row
count.  A failure leaves the original dataset untouched.

Typical use::

    python -m src.data.migrate --dataset bars_intraday --dry-run
    python -m src.data.migrate --dataset bars_intraday
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
from typing import Any, Sequence

from src.data import catalog
from src.data.lake import (
    delete_dataset,
    lake_root,
    read_dataset,
    write_dataset,
)
from src.data.registry import REGISTRY, spec as get_spec

logger = logging.getLogger(__name__)

__all__ = ["current_partitions", "layout_mismatch", "migrate_dataset", "migrate_all"]

# Refuse to rewrite millions of rows on a typo; the caller can insist.
_FORCE_ROW_THRESHOLD = 5_000_000


def current_partitions(name: str) -> tuple[str, ...]:
    """Partition keys actually present on disk, in path order.

    Only the first level under the dataset directory is inspected: that is
    where Hive keys live by construction (``<root>/<dataset>/<key>=<value>/``).
    """
    base = lake_root() / name
    if not base.exists():
        return ()
    keys: dict[int, str] = {}
    for entry in base.iterdir():
        if not entry.is_dir() or "=" not in entry.name:
            continue
        key = entry.name.partition("=")[0]
        keys.setdefault(0, key)
    return tuple(sorted(keys.values()))


def layout_mismatch(name: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """``(on_disk, declared)`` when they disagree, ``None`` when they match."""
    declared = get_spec(name).partitions
    on_disk = current_partitions(name)
    if tuple(on_disk) == tuple(declared):
        return None
    return on_disk, tuple(declared)


def _purge(name: str) -> None:
    """Remove a dataset directory outright (staging, or a parked old layout)."""
    path = lake_root() / name
    if path.exists():
        shutil.rmtree(path)


def _drop_partition_helpers(frame, declared: Sequence[str]):
    """Drop ``year`` / ``date`` helper columns the old writer injected.

    Historical writers added the partition value as a column.  It is not part of
    the declared contract, so keeping it would leave every migrated file with an
    undeclared column — and the console would report it as schema drift forever.
    Only these known helpers are dropped; anything else on disk is left alone.
    """
    helpers = [c for c in ("year", "month") if c in frame.columns and c not in declared]
    return frame.drop(columns=helpers) if helpers else frame


def _backfill_provenance(frame, name: str, declared: Sequence[str]):
    """Add ``source`` / ``vintage`` to rows written before they were recorded.

    The honest values are "unknown" and the moment the file landed (its mtime),
    the only evidence left of when the row was fetched.  Without this every
    migrated dataset keeps a permanent schema-drift alert — and that alert would
    then mask the day a real column goes missing.
    """
    wanted = [c for c in ("source", "vintage") if c in declared and c not in frame.columns]
    if not wanted:
        return frame
    if "source" in wanted:
        frame["source"] = "unknown"
    if "vintage" in wanted:
        stamps = [
            path.stat().st_mtime
            for path in (lake_root() / name).rglob("*.parquet")
            if path.is_file()
        ]
        landed = dt.datetime.fromtimestamp(max(stamps)) if stamps else dt.datetime.now()
        frame["vintage"] = landed
    return frame


def migrate_dataset(
    name: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    rewrite: bool = False,
) -> dict[str, Any]:
    """Rewrite *name* under its declared partitioning.

    Args:
        name: Dataset to migrate.
        dry_run: Report what would happen, change nothing.
        force: Allow a rewrite above :data:`_FORCE_ROW_THRESHOLD` rows.
        rewrite: Rewrite even when the layout already matches — used to
            normalise schema (provenance backfill, helper-column removal) on a
            dataset whose partitioning is already right.

    Returns:
        Report with ``status`` of ``"ok"`` / ``"skipped"`` / ``"mismatch"``,
        the layouts involved and the row count carried over.

    Raises:
        RuntimeError: When the rewrite would lose rows — the original dataset
            is left untouched in that case.
    """
    dataset_spec = get_spec(name)
    if not (lake_root() / name).exists():
        return {"dataset": name, "status": "skipped", "reason": "dataset not written yet"}
    mismatch = layout_mismatch(name)
    if mismatch is None and not rewrite:
        return {"dataset": name, "status": "skipped", "reason": "layout already matches"}

    on_disk, declared = mismatch or (tuple(dataset_spec.partitions), tuple(dataset_spec.partitions))
    frame = read_dataset(name)
    rows = len(frame)
    if rows == 0:
        # Never delete a dataset we could not read: an empty frame here means a
        # corrupt or unreadable partition, not an empty dataset.
        return {
            "dataset": name,
            "status": "skipped",
            "reason": "no rows readable; refusing to drop the dataset",
            "from": list(on_disk),
            "to": list(declared),
        }

    if dry_run:
        return {
            "dataset": name,
            "status": "mismatch",
            "from": list(on_disk),
            "to": list(declared),
            "rows": rows,
            "dry_run": True,
        }

    if rows > _FORCE_ROW_THRESHOLD and not force:
        return {
            "dataset": name,
            "status": "skipped",
            "reason": f"{rows} rows exceeds the safe limit; pass force=True",
            "from": list(on_disk),
            "to": list(declared),
            "rows": rows,
        }

    staging = f"{name}.__migrating__"
    _purge(staging)
    partition = declared[0] if declared else None
    frame = _drop_partition_helpers(frame, dataset_spec.column_names)
    frame = _backfill_provenance(frame, name, dataset_spec.column_names)
    written = write_dataset(
        frame,
        staging,
        partition_by=partition,
        key=dataset_spec.keys,
        sort_by=[c for c in ("symbol", "date") if c in frame.columns] or None,
    )
    if written < rows:
        _purge(staging)
        raise RuntimeError(f"{name}: staged {written} rows but read {rows}; aborted")

    # Swap: move the live dataset aside, promote staging, then drop the old one.
    parked = f"{name}.__old__"
    _purge(parked)
    live = lake_root() / name
    os.replace(live, lake_root() / parked)
    os.replace(lake_root() / staging, live)
    shutil.rmtree(lake_root() / parked, ignore_errors=True)
    delete_dataset(parked)

    catalog.rebuild(name)
    return {
        "dataset": name,
        "status": "ok",
        "from": list(on_disk),
        "to": list(declared),
        "rows": written,
        "partitions": len(list((lake_root() / name).glob("*/data.parquet"))),
    }


def migrate_all(*, dry_run: bool = False, force: bool = False) -> list[dict[str, Any]]:
    """Migrate every declared dataset whose on-disk layout disagrees."""
    reports = []
    for name in REGISTRY:
        if layout_mismatch(name) is None:
            continue
        reports.append(migrate_dataset(name, dry_run=dry_run, force=force))
        logger.info("migrate %s -> %s", name, reports[-1]["status"])
    return reports


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Rewrite lake datasets under their declared partitioning")
    parser.add_argument("--dataset", action="append", default=None, help="dataset name (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--rewrite", action="store_true", help="rewrite even when the layout matches")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.dataset:
        reports = [
            migrate_dataset(n, dry_run=args.dry_run, force=args.force, rewrite=args.rewrite)
            for n in args.dataset
        ]
    else:
        reports = migrate_all(dry_run=args.dry_run, force=args.force)
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
