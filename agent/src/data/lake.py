"""Local data lake: partitioned parquet under one root, with key upserts.

The loader cache (``backtest/loaders/base.py``) is content-addressed by
``source + symbol + timeframe + start_date + end_date``, so the same month of
data fetched through two different date windows lands on disk twice and never
merges.  That is fine for a cache and wrong for a data foundation: nothing can
be joined, deduped, or incrementally extended.

This module is the storage half of the fix.  It stores *datasets* rather than
*responses*::

    <root>/bars/market=cn/year=2024/data.parquet
    <root>/tradability/year=2024/data.parquet
    <root>/membership/board=BK0493/data.parquet
    <root>/calendar/cn.parquet

Writes are upserts on a natural key (default ``symbol + date``), so re-running
an ingest job rewrites the same rows instead of appending a second copy.  Reads
go through DuckDB when it is installed (it is a hard dependency of this
project) and fall back to pandas otherwise.

Nothing here talks to the network; ingest jobs live in ``scripts/`` and in
``src/data/tradability.py``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from src.config.accessor import read_env

logger = logging.getLogger(__name__)

__all__ = [
    "lake_root",
    "dataset_dir",
    "dataset_path",
    "dataset_exists",
    "read_dataset",
    "write_dataset",
    "delete_dataset",
    "read_parquet_file",
    "write_parquet",
    "dataset_bounds",
]

LAKE_ROOT_ENV = "VIBE_TRADING_LAKE_ROOT"
_DATA_FILE = "data.parquet"
_write_lock = threading.Lock()


def lake_root() -> Path:
    """Return the lake root, honouring ``VIBE_TRADING_LAKE_ROOT``.

    Precedence: env var / ``EnvConfig.data.vibe_trading_lake_root``, then
    ``~/.vibe-trading/lake``.  The directory is created on demand by the first
    writer; readers must tolerate it not existing yet.
    """
    configured = ""
    try:
        from src.config.accessor import get_env_config

        configured = getattr(get_env_config().data, "vibe_trading_lake_root", "") or ""
    except Exception:  # noqa: BLE001 — config import is best-effort
        configured = ""
    if not configured:
        configured = read_env(LAKE_ROOT_ENV)
    if configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".vibe-trading" / "lake"


def dataset_dir(name: str, **partitions: object) -> Path:
    """Return the directory for one dataset, optionally under partitions.

    Args:
        name: Dataset name, e.g. ``"bars"`` or ``"tradability"``.
        **partitions: Hive-style partition values, e.g. ``market="cn"``.

    Returns:
        ``<root>/<name>/<k>=<v>/...`` — not created.
    """
    path = lake_root() / name
    for key in sorted(partitions):
        path = path / f"{key}={partitions[key]}"
    return path


def dataset_path(name: str, **partitions: object) -> Path:
    """Return the parquet file path for an unpartitioned dataset."""
    return dataset_dir(name, **partitions) / _DATA_FILE


def dataset_exists(name: str) -> bool:
    """Return True when the dataset has at least one parquet file."""
    base = lake_root() / name
    if not base.exists():
        return False
    return any(base.rglob("*.parquet"))


def read_dataset(
    name: str,
    *,
    columns: Sequence[str] | None = None,
    filters: dict[str, object] | None = None,
    where: tuple[str, Sequence[object]] | None = None,
) -> pd.DataFrame:
    """Read a dataset (all partitions) as one DataFrame.

    Args:
        name: Dataset name.
        columns: Optional column projection.
        filters: Optional partition filters, e.g. ``{"year": 2024}``.  Only
            partition keys are honoured; row-level filtering belongs in
            ``where``.
        where: Optional row predicate as ``(sql, params)`` with ``?``
            placeholders, e.g. ``('"symbol" = ?', ["600519.SH"])``.  It is
            evaluated *inside* DuckDB so a single-symbol chart does not
            materialise five years of the whole market in pandas.  The pandas
            fallback path cannot push it down and applies it after the read, so
            the result is the same either way.

    Returns:
        Concatenated frame with partition columns restored.  Empty frame when
        the dataset does not exist yet.
    """
    base = lake_root() / name
    if not base.exists():
        return pd.DataFrame()
    files = _dataset_files(base, filters)
    if not files:
        return pd.DataFrame()

    clause, params = where if where else ("", [])
    try:
        import duckdb

        con = duckdb.connect()
        try:
            quoted = ", ".join(f"'{f.as_posix()}'" for f in files)
            select = "*" if not columns else ", ".join(f'"{c}"' for c in columns)
            # ``union_by_name`` keeps a partition written before a column was
            # added readable: a backfill that adds a source/vintage column must
            # not render the older partitions unreadable.
            # ``hive_partitioning = false`` avoids a synthetic VARCHAR ``date``
            # column that would collide with the real one; partitions are
            # restored from the paths below instead.
            sql = (
                f"SELECT {select} FROM read_parquet([{quoted}], "
                f"union_by_name = true, hive_partitioning = false)"
            )
            if clause:
                sql += f" WHERE {clause}"
            frame = con.execute(sql, list(params)).fetch_df()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — duckdb missing or version drift
        logger.debug("duckdb read failed (%s); falling back to pandas", exc)
        frames = [read_parquet_file(f, columns=columns) for f in files]
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if frame.empty:
        return frame
    return _restore_partitions(frame, files)


def aggregate_dataset(
    name: str,
    select: str,
    *,
    group_by: str | None = None,
    filters: dict[str, object] | None = None,
    where: tuple[str, Sequence[object]] | None = None,
) -> pd.DataFrame:
    """Run a ``GROUP BY`` style query *inside* the warehouse.

    The heat map asked for "how many bars per symbol per session" over the whole
    market, which used to mean loading 6M rows into pandas and grouping there —
    22 seconds and half a gigabyte to produce 12,000 integers.  The aggregation
    belongs next to the scan.

    Args:
        name: Dataset name.
        select: The projection/aggregation expression, e.g.
            ``'symbol, CAST("date" AS DATE) AS session, count(*) AS bars'``.
        group_by: The matching ``GROUP BY`` expression (``"1, 2"`` for the
            example above).  DuckDB will not infer it.
        filters: Optional partition filters (see :func:`read_dataset`).
        where: Optional row predicate as ``(sql, params)``.

    Returns:
        Whatever the query returns; empty frame when the dataset is absent.
        Callers must not assume partition columns are restored — an aggregate
        that drops them has nothing to restore from.
    """
    base = lake_root() / name
    if not base.exists():
        return pd.DataFrame()
    files = _dataset_files(base, filters)
    if not files:
        return pd.DataFrame()

    import duckdb

    clause, params = where if where else ("", [])
    quoted = ", ".join(f"'{f.as_posix()}'" for f in files)
    sql = (
        f"SELECT {select} FROM read_parquet([{quoted}], "
        f"union_by_name = true, hive_partitioning = false)"
    )
    if clause:
        sql += f" WHERE {clause}"
    if group_by:
        sql += f" GROUP BY {group_by}"
    con = duckdb.connect()
    try:
        return con.execute(sql, list(params)).fetch_df()
    finally:
        con.close()


def _dataset_files(base: Path, filters: dict[str, object] | None) -> list[Path]:
    """Resolve the parquet files of one dataset, honouring partition filters."""
    if filters:
        directories = []
        for pattern in _partition_globs(filters):
            directories.extend(p.parent for p in sorted(base.glob(pattern)))
    else:
        directories = sorted({p.parent for p in base.rglob("*.parquet")})
    return [d / _DATA_FILE for d in directories if (d / _DATA_FILE).exists()]


def _partition_globs(filters: dict[str, object]) -> Iterable[str]:
    """Build glob patterns for the requested partition key/values.

    A value may be a single item or a collection, so a caller narrowing to
    2023-2024 can pass ``{"year": [2023, 2024]}`` instead of reading every
    partition and filtering afterwards.
    """
    keys = sorted(filters)

    def build(index: int, prefix: str) -> Iterable[str]:
        if index == len(keys):
            yield f"{prefix}**/{_DATA_FILE}"
            return
        key = keys[index]
        values = filters[key]
        items = values if isinstance(values, (list, tuple, set, frozenset)) else [values]
        for value in items:
            yield from build(index + 1, f"{prefix}{key}={value}/")

    yield from build(0, "")


def _restore_partitions(frame: pd.DataFrame, files: Sequence[Path]) -> pd.DataFrame:
    """Re-attach hive partition columns dropped by the parquet scan.

    Only columns the scan did not already return are added, and only when every
    file agrees on the value — a multi-partition read must not stamp one
    partition's value onto the others.
    """
    seen: dict[str, set[str]] = {}
    for path in files:
        for key, value in _parse_hive_partitions(path).items():
            seen.setdefault(key, set()).add(value)
    for key, values in seen.items():
        if key in frame.columns or len(values) != 1:
            continue
        frame[key] = next(iter(values))
    return frame


def _parse_hive_partitions(path: Path) -> dict[str, str]:
    """Extract ``k=v`` segments from a dataset path."""
    out: dict[str, str] = {}
    for part in path.parents:
        if "=" not in part.name:
            continue
        key, _, value = part.name.partition("=")
        out[key] = value
    return out


def write_dataset(
    frame: pd.DataFrame,
    name: str,
    *,
    partition_by: str | None = "year",
    key: Sequence[str] = ("symbol", "date"),
    sort_by: Sequence[str] | None = None,
) -> int:
    """Upsert a frame into a dataset.

    Existing rows sharing the natural key are replaced, not duplicated, which
    is what makes re-running an ingest job idempotent.

    Args:
        frame: Rows to write.  Must contain every column in ``key``.
        name: Dataset name.
        partition_by: ``"year"`` (``date`` column's year), ``"date"`` (the
            session, ``YYYY-MM-DD``), or ``None`` for a single file.  The
            partition value is derived for the directory name only — the frame
            is never rewritten, so an intraday timestamp survives intact.
        key: Natural key for the upsert.
        sort_by: Optional sort applied before writing (defaults to ``key``).

    Returns:
        Number of rows written (after dedupe).

    Raises:
        ValueError: When a key column is missing from the frame, or when
            ``partition_by`` is not a column and cannot be derived from ``date``.
    """
    if frame is None or frame.empty:
        return 0

    missing = [c for c in key if c not in frame.columns]
    if missing:
        raise ValueError(f"write_dataset({name!r}) requires key columns {missing}")

    work = frame.copy()
    labels: pd.Series | None = None
    if partition_by is not None:
        labels = _partition_labels(work, name, partition_by)

    sort_cols = list(sort_by or key)
    sort_cols = [c for c in sort_cols if c in work.columns]

    with _write_lock:
        if partition_by is None:
            target_dir = dataset_dir(name)
            written = _upsert_file(work, target_dir, key, sort_cols)
            return written

        written = 0
        for value, group in work.groupby(labels, sort=True):
            target_dir = dataset_dir(name, **{partition_by: value})
            written += _upsert_file(group, target_dir, key, sort_cols)
        return written


def _partition_labels(work: pd.DataFrame, name: str, partition_by: str) -> pd.Series:
    """Derive the hive partition value for every row.

    ``year`` comes from the year of ``date``; ``date`` is the session day.  Both
    are computed into a throwaway series so the stored payload keeps its real
    timestamps (intraday bars are timestamped, not day-stamped).
    """
    if partition_by in work.columns and partition_by not in {"year", "date"}:
        return work[partition_by]
    if "date" not in work.columns:
        raise ValueError(f"write_dataset({name!r}) cannot derive {partition_by!r} without a 'date' column")
    stamps = pd.to_datetime(work["date"], errors="coerce")
    if partition_by == "year":
        return stamps.dt.year.astype("Int64")
    if partition_by == "date":
        return stamps.dt.strftime("%Y-%m-%d")
    raise ValueError(f"write_dataset({name!r}) does not know how to partition by {partition_by!r}")


def _upsert_file(
    frame: pd.DataFrame,
    target_dir: Path,
    key: Sequence[str],
    sort_cols: Sequence[str],
) -> int:
    """Merge one partition with what is already on disk and write atomically."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _DATA_FILE

    if target.exists():
        try:
            existing = read_parquet_file(target)
            frame = _carry_over_nulls(existing, frame, key)
            frame = pd.concat([existing, frame], ignore_index=True)
        except Exception as exc:  # noqa: BLE001 — corrupt part is not fatal
            logger.warning("lake partition %s unreadable (%s); rewriting", target, exc)

    frame = frame.drop_duplicates(subset=list(key), keep="last")
    if sort_cols:
        frame = frame.sort_values(list(sort_cols), kind="stable").reset_index(drop=True)

    tmp = target.with_suffix(".parquet.tmp")
    write_parquet(frame, tmp)
    os.replace(tmp, target)
    return len(frame)


def _carry_over_nulls(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    key: Sequence[str],
) -> pd.DataFrame:
    """Fill nulls in *incoming* from the stored row with the same key.

    A write is a row replace, but "I did not fetch this column" is not the same
    statement as "this column is empty".  Tencent has no adjustment factor and no
    turnover amount; re-pulling a window from it therefore used to blank the
    ``adj_factor`` that a baostock repair had filled, and a five-year appendix of
    corporate-action corrections disappeared without a word.

    So the column contract for :func:`write_dataset` is: **a null never
    overwrites a value.**  Clearing a column on purpose is a different operation
    (drop the partition, or rewrite it), which is the right way round — silent
    loss from an unrelated refresh is the failure mode that actually happens.
    """
    if incoming.empty or existing.empty:
        return incoming
    keys = [column for column in key if column in existing.columns]
    if not keys:
        return incoming
    shared = [
        column
        for column in incoming.columns
        if column in existing.columns and column not in keys
    ]
    nullable = [column for column in shared if bool(incoming[column].isna().any())]
    if not nullable:
        return incoming
    previous = existing[keys + nullable].drop_duplicates(subset=keys, keep="last")
    # A parquet round-trip can come back at a different datetime resolution than
    # the in-memory frame (``us`` vs ``ns``); merging on those raises instead of
    # matching, so pin them to one dtype first.
    stamps = [
        column
        for column in keys
        if pd.api.types.is_datetime64_any_dtype(previous[column])
        or pd.api.types.is_datetime64_any_dtype(incoming[column])
    ]
    if stamps:
        incoming = incoming.copy()
        for column in stamps:
            previous[column] = pd.to_datetime(previous[column])
            incoming[column] = pd.to_datetime(incoming[column])
    merged = incoming.merge(previous, on=keys, how="left", suffixes=("", "__stored"))
    for column in nullable:
        stored = merged.pop(f"{column}__stored")
        merged[column] = merged[column].where(merged[column].notna(), stored)
    return merged


# ---------------------------------------------------------------------------
# Parquet IO
# ---------------------------------------------------------------------------
#
# The project writes parquet through DuckDB, not pyarrow: pyarrow is not a
# dependency of this package (``backtest/loaders/base.py`` does the same), so
# calling ``DataFrame.to_parquet`` fails on a clean install.


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write a frame to parquet via DuckDB (no pyarrow requirement).

    Datetime columns are normalised to ``datetime64[ns]`` first.  A frame with
    two different datetime resolutions (say a ``date`` at ``ns`` and a
    ``vintage`` at ``us``) otherwise makes DuckDB's pandas scan collapse the
    ``ns`` column to ``DATE`` — silently truncating intraday timestamps to
    midnight, which then dedupes every bar of a session into one row.
    """
    import duckdb

    frame = _normalize_datetimes(frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.register("lake_frame", frame)
        con.execute(f"COPY lake_frame TO {_sql_string(str(path))} (FORMAT PARQUET)")
    finally:
        con.close()


def _normalize_datetimes(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast every datetime column to the same resolution before writing."""
    stamps = [c for c in frame.columns if pd.api.types.is_datetime64_any_dtype(frame[c])]
    if not stamps:
        return frame
    out = frame.copy()
    for column in stamps:
        out[column] = out[column].astype("datetime64[ns]")
    return out


def read_parquet_file(path: Path, *, columns: Sequence[str] | None = None) -> pd.DataFrame:
    """Read one parquet file via DuckDB, falling back to pandas when available.

    ``hive_partitioning = false`` is not optional here.  DuckDB treats a path
    containing ``date=2024-01-02`` as a Hive partition and **replaces** the real
    ``date`` column with the partition value, which silently truncates intraday
    timestamps to midnight.  Reading a partition's file must see what is in the
    file, not what its directory is called.
    """
    try:
        import duckdb

        con = duckdb.connect()
        try:
            select = "*" if not columns else ", ".join(f'"{c}"' for c in columns)
            return con.execute(
                f"SELECT {select} FROM read_parquet("
                f"{_sql_string(str(path))}, hive_partitioning = false)"
            ).fetch_df()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — duckdb unavailable
        logger.debug("duckdb parquet read failed for %s (%s)", path, exc)
        return pd.read_parquet(path, columns=list(columns) if columns else None)


def _sql_string(value: str) -> str:
    """Quote a filesystem path for a DuckDB SQL literal."""
    return "'" + value.replace("'", "''") + "'"


def delete_dataset(name: str) -> bool:
    """Remove a dataset directory tree.  Returns True when something was removed."""
    import shutil

    base = lake_root() / name
    if not base.exists():
        return False
    shutil.rmtree(base)
    return True


def symbol_spans(name: str, *, column: str = "date") -> dict[str, tuple[str, str]]:
    """Return ``{symbol: (first, last)}`` for every symbol in a dataset.

    This is what makes a full-market backfill resumable: run it, watch it stop
    (deadline, Ctrl-C, a flaky feed), run it again, and it skips the symbols that
    already hold the requested window instead of re-pulling 5,000 series.  One
    aggregate over the parquet scan — the alternative is loading every row just
    to compute a min and a max per group.

    Both ends matter.  A symbol whose last session reaches the window's end but
    whose first session starts inside it is a symbol seeded by a short cold-start
    run, not a covered one; skipping on the end date alone would let a 30-session
    lake masquerade as five years of history.
    """
    base = lake_root() / name
    if not base.exists():
        return {}
    files = sorted(base.rglob("*.parquet"))
    if not files:
        return {}
    try:
        import duckdb

        con = duckdb.connect()
        try:
            quoted = ", ".join(f"'{f.as_posix()}'" for f in files)
            rows = con.execute(
                f'SELECT symbol, min("{column}"), max("{column}") FROM read_parquet('
                f"[{quoted}], union_by_name = true, hive_partitioning = false) "
                f"GROUP BY symbol"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - duckdb missing or column absent
        logger.debug("symbol_spans(%s) failed: %s", name, exc)
        return {}
    out: dict[str, tuple[str, str]] = {}
    for symbol, first, last in rows:
        if symbol is None or first is None or last is None:
            continue
        out[str(symbol)] = (str(first)[:10], str(last)[:10])
    return out


def dataset_bounds(
    name: str, *, column: str = "date"
) -> tuple[str | None, str | None, int]:
    """Return ``(min, max, rows)`` for *column* across a dataset.

    Ingest jobs need a watermark ("what is already stored") before deciding what
    to fetch.  Reading the whole dataset to find that would defeat the point, so
    it is one aggregate pushed into the parquet scan.  A missing dataset answers
    ``(None, None, 0)`` instead of raising: a cold lake is a normal state.
    """
    base = lake_root() / name
    if not base.exists():
        return None, None, 0
    files = sorted(base.rglob("*.parquet"))
    if not files:
        return None, None, 0
    try:
        import duckdb

        con = duckdb.connect()
        try:
            quoted = ", ".join(f"'{f.as_posix()}'" for f in files)
            row = con.execute(
                f'SELECT min("{column}"), max("{column}"), count(*) FROM read_parquet('
                f"[{quoted}], union_by_name = true, hive_partitioning = false)"
            ).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - duckdb missing or column absent
        logger.debug("dataset_bounds(%s) failed: %s", name, exc)
        return None, None, 0
    if not row:
        return None, None, 0
    low, high, count = row
    return (
        str(low)[:10] if low is not None else None,
        str(high)[:10] if high is not None else None,
        int(count or 0),
    )
