"""Catalog: what the lake actually contains, and how healthy it is.

The lake itself only knows how to store rows.  This module answers the
questions a data *console* has to answer without reading the whole dataset:
how many rows, how many symbols, which sessions, which partitions, and did any
integrity predicate ever fire.

Two write paths, deliberately different in cost:

* :func:`touch` — called after every incremental ingest.  O(1): stamps
  ``last_write`` / ``last_source`` and marks the manifest dirty.  Never fakes a
  row count.
* :func:`rebuild` — full scan through DuckDB (parquet footers, so seconds even
  for hundreds of files).  Refreshes counts, coverage and the quality panel.

The manifest lives at ``<lake>/_catalog/<dataset>.json`` next to the data, so a
copied lake stays self-describing and a wiped manifest is always recoverable by
one rebuild.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "catalog_dir",
    "manifest_path",
    "read_manifest",
    "write_manifest",
    "rebuild",
    "rebuild_all",
    "touch",
    "has_data",
    "file_count",
    "parquet_files",
]

SCHEMA_VERSION = 1
_CATALOG_DIRNAME = "_catalog"
# Guard rails so a rebuild never turns into a full-table scan by accident.
_DUP_SCAN_ROW_LIMIT = 20_000_000
_PARTITION_SCAN_LIMIT = 64
_DATE_COLUMNS = ("date", "trade_date", "datetime", "ts")


def _lake_root() -> Path:
    from src.data.lake import lake_root

    return lake_root()


def catalog_dir() -> Path:
    """Directory holding every dataset manifest."""
    return _lake_root() / _CATALOG_DIRNAME


def manifest_path(name: str) -> Path:
    """Manifest file for *name* (not created)."""
    return catalog_dir() / f"{name}.json"


def read_manifest(name: str) -> dict[str, Any] | None:
    """Return the stored manifest, or ``None`` when absent/unreadable."""
    path = manifest_path(name)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("catalog manifest %s unreadable: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def write_manifest(name: str, payload: dict[str, Any]) -> None:
    """Atomically persist a manifest (tmp + replace, UTF-8)."""
    path = manifest_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def touch(
    name: str,
    *,
    rows: int = 0,
    source: str = "",
    job: str = "",
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """Record an incremental write without scanning the dataset.

    Returns the updated manifest so a caller can chain it into a response.
    """
    payload = read_manifest(name)
    if payload is None:
        if not _has_parquet(name):
            return None
        payload = {"dataset": name, "schema_version": SCHEMA_VERSION}

    stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
    payload["dataset"] = name
    payload["schema_version"] = SCHEMA_VERSION
    payload["last_write"] = stamp
    if source:
        payload["last_source"] = source
    if job:
        payload["last_job"] = job
    if rows:
        base = int(payload.get("rows_estimate") or payload.get("rows") or 0)
        payload["rows_estimate"] = base + int(rows)
    payload["dirty"] = True
    write_manifest(name, payload)
    return payload


def rebuild(name: str) -> dict[str, Any]:
    """Scan one dataset and rewrite its manifest.

    Never raises for a missing dataset: an empty lake is a valid state and the
    console needs to render it ("no data yet") rather than a 500.
    """
    from src.data.registry import spec

    dataset_spec = spec(name)
    generated = dt.datetime.now().isoformat(timespec="seconds")
    files = _parquet_files(name)
    if not files:
        payload = {
            "dataset": name,
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "rows": 0,
            "symbols": 0,
            "date_min": None,
            "date_max": None,
            "files": 0,
            "bytes": 0,
            "partitions": [],
            "columns": [],
            "quality": {"dup_keys": None, "checks": [], "note": "dataset is empty"},
            "dirty": False,
        }
        write_manifest(name, payload)
        return payload

    payload: dict[str, Any] = {
        "dataset": name,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "files": len(files),
        "bytes": sum(_safe_size(f) for f in files),
        "dirty": False,
    }

    try:
        import duckdb
    except Exception as exc:  # pragma: no cover - duckdb is a hard dependency
        logger.warning("duckdb unavailable, catalog scan skipped: %s", exc)
        payload["quality"] = {"dup_keys": None, "checks": [], "note": f"duckdb unavailable: {exc}"}
        write_manifest(name, payload)
        return payload

    con = duckdb.connect()
    try:
        scan = _scan_expr(files)
        described = con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()
        observed = [(str(row[0]), str(row[1])) for row in described]
        columns = [c for c, _ in observed]

        rows = int(con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0])
        payload["rows"] = rows
        payload["columns"] = [{"name": c, "type": t} for c, t in observed]
        payload["schema_drift"] = _schema_drift(dataset_spec.column_names, columns)

        if "symbol" in columns:
            payload["symbols"] = int(
                con.execute(f"SELECT count(DISTINCT symbol) FROM {scan}").fetchone()[0] or 0
            )

        date_col = next((c for c in _DATE_COLUMNS if c in columns), None)
        if date_col:
            span = con.execute(
                f'SELECT min(CAST("{date_col}" AS DATE)), max(CAST("{date_col}" AS DATE)) FROM {scan}'
            ).fetchone()
            payload["date_column"] = date_col
            payload["date_min"] = span[0].isoformat() if span and span[0] else None
            payload["date_max"] = span[1].isoformat() if span and span[1] else None

        payload["quality"] = _quality(con, scan, dataset_spec, columns, rows)
        payload["partitions"] = _partitions(con, files) if len({f.parent for f in files}) <= _PARTITION_SCAN_LIMIT else [
            {"name": _partition_label(f), "rows": None, "files": 1} for f in files
        ]
    finally:
        con.close()

    if name == "calendar" and payload.get("date_max"):
        # The calendar is one file at the dataset root; keep a row count for the
        # "sessions" KPI without re-scanning on every page load.
        payload["sessions"] = payload["rows"]

    write_manifest(name, payload)
    return payload


def has_data(name: str) -> bool:
    """True when the dataset directory holds at least one parquet file."""
    return _has_parquet(name)


def parquet_files(name: str) -> list[Path]:
    """Every parquet file of *name*, sorted (empty when absent)."""
    return _parquet_files(name)


def file_count(name: str) -> int:
    """Number of parquet files backing *name* (a cheap freshness signal)."""
    base = _lake_root() / name
    if not base.exists():
        return 0
    return sum(1 for _ in base.rglob("*.parquet"))


def rebuild_all(names: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Rebuild several manifests; unknown datasets are skipped, not fatal."""
    from src.data.registry import REGISTRY

    targets = list(names) if names is not None else list(REGISTRY)
    out: dict[str, dict[str, Any]] = {}
    for name in targets:
        if name not in REGISTRY:
            logger.warning("catalog rebuild skipped unknown dataset %r", name)
            continue
        try:
            out[name] = rebuild(name)
        except Exception as exc:  # noqa: BLE001 - one bad dataset must not abort the rest
            logger.exception("catalog rebuild failed for %s", name)
            out[name] = {"dataset": name, "error": str(exc)}
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _has_parquet(name: str) -> bool:
    base = _lake_root() / name
    return base.exists() and any(base.rglob("*.parquet"))


def _parquet_files(name: str) -> list[Path]:
    base = _lake_root() / name
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.parquet") if p.is_file())


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _scan_expr(files: Sequence[Path]) -> str:
    """Build a ``read_parquet([...])`` expression with quoted POSIX paths.

    ``hive_partitioning = false`` is deliberate: the files already carry the
    partition value as a real column (``date``), and letting DuckDB infer a
    ``date=...`` partition would add a second, VARCHAR ``date`` that collides
    with it.  Partition columns are restored from the paths by the callers that
    need them.  ``union_by_name`` keeps an older partition readable after a
    column is added.
    """
    quoted = ", ".join(f"'{p.as_posix().replace(chr(39), chr(39) * 2)}'" for p in files)
    return f"read_parquet([{quoted}], union_by_name = true, hive_partitioning = false)"


def _schema_drift(declared: Sequence[str], observed: Sequence[str]) -> dict[str, list[str]]:
    declared_set, observed_set = set(declared), set(observed)
    return {
        "missing": sorted(declared_set - observed_set),
        "extra": sorted(observed_set - declared_set - {"year"}),
    }


def _quality(
    con: Any,
    scan: str,
    spec: Any,
    columns: Sequence[str],
    rows: int,
) -> dict[str, Any]:
    """Duplicate-key count plus every integrity predicate the spec declares."""
    keys = spec.keys
    dup: int | None = None
    if all(k in columns for k in keys):
        if rows <= _DUP_SCAN_ROW_LIMIT:
            key_list = ", ".join(f'"{k}"' for k in keys)
            try:
                dup = int(
                    con.execute(
                        f"SELECT count(*) FROM (SELECT {key_list}, count(*) AS c FROM {scan} "
                        f"GROUP BY {key_list} HAVING c > 1)"
                    ).fetchone()[0]
                )
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                dup = None
                logger.debug("dup-key scan failed: %s", exc)
        else:
            dup = None

    checks: list[dict[str, Any]] = []
    for check_id, predicate in spec.checks:
        try:
            violations = int(con.execute(f"SELECT count(*) FROM {scan} WHERE {predicate}").fetchone()[0])
            checks.append({"id": check_id, "violations": violations})
        except Exception as exc:  # noqa: BLE001 - column may not exist yet
            checks.append({"id": check_id, "violations": None, "error": str(exc).splitlines()[0]})

    return {"dup_keys": dup, "checks": checks}


def _partitions(con: Any, files: Sequence[Path]) -> list[dict[str, Any]]:
    """Row count per partition directory (bounded by caller)."""
    grouped: dict[str, list[Path]] = {}
    for path in files:
        grouped.setdefault(_partition_label(path), []).append(path)
    out: list[dict[str, Any]] = []
    for label, members in sorted(grouped.items()):
        rows: int | None
        try:
            rows = int(con.execute(f"SELECT count(*) FROM {_scan_expr(members)}").fetchone()[0])
        except Exception:  # noqa: BLE001
            rows = None
        out.append(
            {
                "name": label,
                "rows": rows,
                "files": len(members),
                "bytes": sum(_safe_size(p) for p in members),
            }
        )
    return out


def _partition_label(path: Path) -> str:
    parts = [p.name for p in path.parents if "=" in p.name]
    return "/".join(reversed(parts)) or "(root)"
