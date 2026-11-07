"""Bar storage in the lake: daily and intraday OHLCV, keyed by symbol + timestamp.

The loaders fetch from the network; this module is where bars persist.  It
exists because the free intraday feeds are *windows*, not archives: Tencent's
``mkline`` returns 320 bars, Sina a few thousand, and neither accepts a date
range.  A single request can never produce a year of 5-minute bars — but a
request per day, merged into the lake, eventually does.

Storage layout — partition comes from the dataset declaration, not from this
module, so a layout change is a registry edit::

    <root>/bars_daily/year=2024/data.parquet      # one file per year is fine
    <root>/bars_intraday/date=2024-01-02/data.parquet   # one file per session

Intraday is partitioned **per session** on purpose: a settled session never
changes again, so a backfill appends a file instead of rewriting the year.  With
``year=`` an add-a-day job on the full market rewrote ~58M rows; with ``date=``
it writes 240.

Rows are long-format (``symbol, date, interval, open, high, low, close,
volume``) and upserted on ``(symbol, interval, date)``, so re-running an ingest
job over an overlapping window rewrites the same bars instead of appending
duplicates.  This is why re-fetching a sliding window is safe and why
:func:`coverage` can report how much history has actually accumulated.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Mapping, Sequence

import pandas as pd

from src.data.lake import write_dataset

logger = logging.getLogger(__name__)

__all__ = [
    "DATASET_DAILY",
    "DATASET_INTRADAY",
    "dataset_for",
    "save_bars",
    "save_long_bars",
    "load_bars",
    "session_counts",
    "bars_by_symbol",
    "coverage",
    "watermark",
]

DATASET_DAILY = "bars_daily"
DATASET_INTRADAY = "bars_intraday"

_OHLCV = ["open", "high", "low", "close", "volume"]
# Provenance columns every bar carries; without them a row cannot say where it
# came from or when it was fetched, which is what the console's schema-check
# flagged as drift.
_PROVENANCE = ["source", "vintage"]


def dataset_for(interval: str) -> str:
    """Return the dataset name that holds bars of *interval*."""
    from backtest.loaders._intervals import is_intraday, normalize_interval

    return DATASET_INTRADAY if is_intraday(normalize_interval(interval)) else DATASET_DAILY


def save_bars(
    frames: Mapping[str, pd.DataFrame],
    interval: str,
    *,
    source: str = "",
) -> int:
    """Upsert ``{symbol: OHLCV frame}`` into the lake.

    Args:
        frames: Symbol -> frame with a DatetimeIndex and OHLCV columns.
        interval: Bar size the frames are at; stored as a column so one
            dataset can hold 5-minute and hourly bars side by side.
        source: Loader name, recorded for provenance (``""`` when unknown).

    Returns:
        Number of rows written after dedupe.

    Raises:
        ValueError: When *interval* is not a recognised bar size.
    """
    from backtest.loaders._intervals import normalize_interval
    from src.data.registry import spec as get_spec

    token = normalize_interval(interval)
    dataset = dataset_for(token)
    dataset_spec = get_spec(dataset)

    rows = []
    for symbol, frame in frames.items():
        if frame is None or frame.empty:
            continue
        if not isinstance(frame.index, pd.DatetimeIndex):
            logger.warning("save_bars: %s has a non-datetime index; skipped", symbol)
            continue
        part = frame.reset_index()
        stamp_col = part.columns[0]
        part = part.rename(columns={stamp_col: "date"})
        part["symbol"] = symbol
        part["interval"] = token
        if source:
            part["source"] = source
        part["vintage"] = dt.datetime.now()
        rows.append(part)

    if not rows:
        return 0

    frame = pd.concat(rows, ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    if frame.empty:
        return 0

    return save_long_bars(frame, token, source=source)


def save_long_bars(frame: pd.DataFrame, interval: str, *, source: str = "") -> int:
    """Upsert an already-long bar frame (``symbol, date, OHLCV…``) into the lake.

    :func:`save_bars` takes the per-symbol shape the loaders return; an ingester
    that fetched one *session* for the whole market (tushare ``daily``, or one
    exchange-wide snapshot) has the same rows in the other orientation, and
    rebuilding thousands of per-symbol frames just to call the other entry point
    would be pure overhead.  Both funnel into this function so the column
    contract, provenance stamping and partitioning are written in one place.

    Rows missing ``symbol`` or ``date`` are dropped rather than written under a
    guess — a bar that cannot name its key is not a bar.
    """
    from backtest.loaders._intervals import normalize_interval
    from src.data.registry import spec as get_spec

    if frame is None or frame.empty:
        return 0

    token = normalize_interval(interval)
    dataset = dataset_for(token)
    dataset_spec = get_spec(dataset)

    work = frame.copy()
    if "symbol" not in work.columns or "date" not in work.columns:
        raise ValueError("save_long_bars requires 'symbol' and 'date' columns")
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date", "symbol"])
    if work.empty:
        return 0

    work["symbol"] = work["symbol"].astype(str)
    work["interval"] = token
    if source:
        work["source"] = source
    if "vintage" not in work.columns:
        work["vintage"] = dt.datetime.now()

    # Persist exactly the declared contract: dropping a column here is what made
    # the console report schema drift (source/vintage never reached disk).
    keep = [c for c in dataset_spec.column_names if c in work.columns]
    extra = [c for c in _OHLCV + _PROVENANCE if c in work.columns and c not in keep]
    work = work[keep + extra]

    partition = dataset_spec.partitions[0] if dataset_spec.partitions else None
    key = _key_for(dataset_spec.keys, work)
    # ``write_dataset`` returns the size of the merged partition, which grows to
    # the whole year's row count and would triple-count when three symbols are
    # written one after another.  Callers report "how much did this job add", so
    # the contract here is the deduped input size, not the file size.
    contributed = int(len(work.drop_duplicates(subset=list(key))))
    write_dataset(
        work,
        dataset,
        partition_by=partition,
        key=key,
        sort_by=["symbol", "date"],
    )
    return contributed


def watermark(interval: str = "1d") -> str | None:
    """Latest session already stored for *interval* (``None`` when empty).

    The incremental contract: "fetch what is missing after this date".  Reading
    only the max date keeps a daily job from re-pulling five years every night.
    """
    from src.data.lake import dataset_bounds

    _, high, rows = dataset_bounds(dataset_for(interval))
    return high if rows else None


def _key_for(keys: Sequence[str], frame: pd.DataFrame) -> tuple[str, ...]:
    """Natural key filtered to the columns the frame actually has.

    ``bars_daily`` keys on ``(symbol, date)`` while intraday keys on
    ``(symbol, interval, date)``; both live in the registry, so neither this
    module nor a future dataset needs its own hard-coded tuple.
    """
    present = tuple(k for k in keys if k in frame.columns)
    return present or ("symbol", "date")


def session_counts(
    interval: str,
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    by_month: bool = False,
) -> pd.DataFrame:
    """Sessions with data per ``(symbol, bucket)``, aggregated in the warehouse.

    The coverage heat map needs this shape for the whole market.  Deriving it
    from :func:`load_bars` means materialising every bar in pandas to count it —
    six million rows and 18 seconds to produce a few thousand integers.

    Args:
        by_month: Bucket by calendar month instead of by session, used when the
            window is too long to draw one column per session.

    Returns:
        Frame with ``symbol, bucket, covered`` where ``covered`` is the number of
        distinct sessions that have at least one bar; empty when nothing matches.
    """
    from backtest.loaders._intervals import normalize_interval
    from src.data import lake
    from src.data.registry import spec as get_spec

    token = normalize_interval(interval)
    dataset = dataset_for(token)
    dataset_spec = get_spec(dataset)

    clauses: list[str] = ['("interval" = ? OR "interval" IS NULL)']
    params: list[object] = [token]
    if start:
        clauses.append('"date" >= ?')
        params.append(pd.Timestamp(start))
    if end:
        clauses.append('"date" < ?')
        params.append(pd.Timestamp(end) + pd.Timedelta(days=1))
    if symbols:
        wanted = sorted({str(s) for s in symbols})
        clauses.append(f'"symbol" IN ({", ".join("?" for _ in wanted)})')
        params.extend(wanted)

    filters: dict[str, object] | None = None
    if dataset_spec.partitions == ("year",) and start and end:
        filters = {"year": list(range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1))}

    session = 'CAST("date" AS DATE)'
    bucket = (
        f"strftime({session}, '%Y-%m')" if by_month else session
    )
    frame = lake.aggregate_dataset(
        dataset,
        f"symbol, {bucket} AS bucket, count(DISTINCT {session}) AS covered",
        group_by="1, 2",
        filters=filters,
        where=(" AND ".join(clauses), params),
    )
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "bucket", "covered"])
    frame["bucket"] = frame["bucket"].astype(str)
    return frame


def load_bars(
    interval: str,
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Read stored bars as one long frame, filtered to the requested slice.

    The filter is pushed into the parquet scan rather than applied after the
    read: once ``bars_daily`` holds the whole market for five years it is ~7M
    rows, and a chart for one symbol must not materialise all of them.  Year
    partitions outside the requested range are skipped entirely; ``symbol`` and
    the date bounds become a SQL predicate.

    Args:
        interval: Bar size; filtered by the ``interval`` column.
        symbols: Optional symbol whitelist.
        start: Inclusive lower bound (``YYYY-MM-DD``).
        end: Inclusive upper bound (``YYYY-MM-DD``); the whole end day is kept.

    Returns:
        Long frame sorted by ``symbol, date``. Empty when nothing is stored.
    """
    from backtest.loaders._intervals import normalize_interval
    from src.data import lake
    from src.data.registry import spec as get_spec

    token = normalize_interval(interval)
    dataset = dataset_for(token)
    dataset_spec = get_spec(dataset)

    clauses: list[str] = []
    params: list[object] = []
    # Partitions written before the ``interval`` column existed carry NULL; they
    # are still this interval's data, so they pass.
    clauses.append('("interval" = ? OR "interval" IS NULL)')
    params.append(token)
    if start:
        clauses.append('"date" >= ?')
        params.append(pd.Timestamp(start))
    if end:
        clauses.append('"date" < ?')
        params.append(pd.Timestamp(end) + pd.Timedelta(days=1))
    if symbols:
        wanted = sorted({str(s) for s in symbols})
        placeholders = ", ".join("?" for _ in wanted)
        clauses.append(f'"symbol" IN ({placeholders})')
        params.extend(wanted)

    filters: dict[str, object] | None = None
    if dataset_spec.partitions == ("year",) and (start or end):
        first = pd.Timestamp(start).year if start else None
        last = pd.Timestamp(end).year if end else None
        if first is not None and last is not None:
            filters = {"year": list(range(first, last + 1))}

    frame = lake.read_dataset(
        dataset, filters=filters, where=(" AND ".join(clauses), params)
    )
    if frame.empty:
        return frame

    if "date" in frame.columns:
        frame = frame.assign(date=pd.to_datetime(frame["date"], errors="coerce"))
        frame = frame.dropna(subset=["date"])

    return frame.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def bars_by_symbol(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split a long frame from :func:`load_bars` into per-symbol OHLCV frames."""
    out: dict[str, pd.DataFrame] = {}
    if frame is None or frame.empty:
        return out
    columns = [c for c in ("open", "high", "low", "close", "volume") if c in frame.columns]
    for symbol, part in frame.groupby("symbol", sort=True):
        part = part.set_index("date").sort_index()
        out[str(symbol)] = part[columns]
    return out


def coverage(
    interval: str, *, symbols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Summarise how much history the lake holds per symbol.

    This is the number that matters when the sources only serve recent bars:
    it reports the accumulated span, not what a single request happened to
    return.

    Args:
        interval: Bar size.
        symbols: Optional symbol whitelist.

    Returns:
        Frame with ``symbol, bars, first, last, sessions`` sorted by symbol.
    """
    wanted = set(symbols) if symbols is not None else None
    frame = load_bars(interval, symbols=sorted(wanted) if wanted else None)
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "bars", "first", "last", "sessions"])

    grouped = frame.groupby("symbol")["date"]
    summary = pd.DataFrame(
        {
            "bars": grouped.size(),
            "first": grouped.min(),
            "last": grouped.max(),
            "sessions": grouped.apply(lambda s: s.dt.date.nunique()),
        }
    ).reset_index()
    return summary.sort_values("symbol", kind="stable").reset_index(drop=True)
