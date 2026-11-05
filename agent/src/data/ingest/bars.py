"""Bar ingestion: daily full-market sessions and intraday sliding windows.

Two sources, one storage contract
---------------------------------
The lake wants *unadjusted* OHLCV plus an adjustment factor (see
``bars_daily`` in the registry): every downstream consumer — tradability bands,
backtests, relative strength — needs the price the exchange actually printed,
not a series silently re-based on today's dividends.

* **Tushare ``daily``** is the preferred path: one call per trade date returns
  the whole market, so a year of history is ~240 calls instead of ~240 × 5000.
  ``adj_factor`` comes from its own endpoint and never blocks the bars: a plan
  without that permission still gets a correct unadjusted series, with the
  factor left null and a note in the report.
* **Tencent ``fqkline`` with an empty adjustment slot** serves raw daily prices
  for a whole date range per symbol (up to ~2000 sessions), which makes it the
  practical backfill path when no token is configured.  It carries no turnover,
  so ``amount`` is left null there.
* **Eastmoney ``push2his`` with ``fqt=0``** is the third path: raw prices *and*
  ``amount``, but its CDN is unreachable behind several corporate proxies, which
  is why it is not the first fallback.

Tencent's *adjusted* daily endpoint is deliberately never used here: ``qfq``
prices look identical to the untrained eye and would quietly poison every band
and backtest reading unadjusted data.

Units are fixed here, once, because the sources disagree (``volume`` in 手,
``amount`` in 元) and a mixed-unit lake is worse than an empty one:

* ``volume`` — 手 (lots of 100 shares), matching the intraday dataset;
* ``amount`` — 元 (tushare returns 千元, multiplied on the way in).

Incremental by default
----------------------
Without an explicit range the job asks the lake for its watermark and fetches
only the sessions after it.  A cold lake starts with a bounded window instead of
five years in one run, so the console gets a working chart immediately and the
rest arrives as repeated jobs — visible, resumable, never a half-written year.

Full-market backfill
--------------------
``universe_mode="full"`` swaps the bounded symbol list for the whole listed
market (see :mod:`src.data.ingest.universe`) and lifts the session cap, which is
what a five-year backfill needs.  Three things make that practical rather than
theoretical:

* **Concurrency.** One request per symbol is ~0.2s; 5,500 of them serially is
  20 minutes of wall clock for 80 seconds of network.  Fetches run on a small
  pool with a bounded in-flight window, so memory stays flat.
* **Batched writes.** ``lake.write_dataset`` is read-modify-write per partition.
  Writing symbol by symbol would re-read and rewrite the same year file
  thousands of times; :class:`_BarSink` buffers per year and writes each
  partition a handful of times instead.
* **Resume by watermark.** Symbols already reaching the target session are
  skipped, so a run that stops at its deadline, or dies, is finished by simply
  running it again instead of starting over.  That is only sound because a bar
  write upserts a whole range: reaching the end date implies the series behind
  it is stored.

Repairing the factor column
---------------------------
Neither Tencent nor Sina publishes an adjustment factor, so a backfill driven by
them lands five years of correct *unadjusted* prices with ``adj_factor`` null —
enough for bands and liquidity, useless for any adjusted-return question.
``mode="adj_factor"`` fills that column in place from baostock's cumulative
``backAdjustFactor`` (one call per symbol, whole history) and rewrites only the
rows it touches: the prices never move, so the repair cannot corrupt a series it
did not fetch.  Tushare's ``adj_factor`` would answer a whole market per call,
but a standard plan caps that endpoint at one call per hour.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["daily", "intraday", "default_universe"]

# A cold run pulls a month, not five years: enough for a chart, the coverage
# matrix and a first tradability panel, small enough to finish inside a job.
DEFAULT_BACKFILL_SESSIONS = 30
# One job never asks for more than this many sessions; the CLI can loop.
MAX_SESSIONS_PER_RUN = 250
# A full-market backfill is the one case that legitimately wants more, and its
# ceiling is the retention window rather than a job budget.
MAX_SESSIONS_FULL = 6 * 260
# Rows buffered before a year partition is rewritten.  400k rows is ~50MB of
# frames: small enough to stay comfortably in memory next to the partition being
# merged, large enough that a five-year, 5,500-symbol backfill rewrites each year
# file about ten times instead of fifty.
_FLUSH_ROWS = 400_000
# Tencent answers ~60 requests/second across 12 connections; more than that and
# the host starts dropping connections, which costs more than it saves.
_DEFAULT_WORKERS = 8
_MAX_WORKERS = 16
# Start-rate ceiling shared by every worker.  Tuned down from what the host will
# accept: a burst gets the whole source rejected (HTTP 501) and costs far more
# than the seconds it saves.
_REQUEST_PER_SECOND = 8.0

_VOLUME_UNIT_NOTE = "volume 单位为手、amount 单位为元"

# Symbols fetched before the factor repair checkpoints.  baostock answers one
# symbol per call (~0.15s), so 600 is ~90 seconds of wall clock and one
# read-modify-write per year partition per chunk.
_ADJ_CHUNK_SYMBOLS = 600


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _elapsed(started: float, deadline_s: float) -> bool:
    return deadline_s > 0 and (time.monotonic() - started) >= deadline_s


def _canonical(code: str) -> str:
    """Return the project's ``600519.SH`` form for a bare or suffixed code.

    Reuses the correlation module's rule rather than restating it: two copies of
    "6 → SH, 4/8 → BJ, else SZ" is exactly how symbols drift apart.
    """
    from backtest.correlation import _normalize_symbol, infer_market

    text = str(code).strip()
    if not text:
        return ""
    return _normalize_symbol(text, infer_market(text))


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


def _resolve_window(
    start: str | None,
    end: str | None,
    *,
    interval: str,
    refresh: bool,
) -> tuple[str | None, str, list[str]]:
    """Return ``(start, end, notes)`` for the sessions to fetch.

    ``start`` is ``None`` when the lake already covers the window — the caller
    must then return without touching the network.  ``refresh`` means "re-pull
    the window even though the lake covers it" (used after a source correction).
    """
    from src.data import bars as bar_store
    from src.data import calendar
    from src.data import hub

    as_of = hub.resolve_as_of().date  # ISO string, not a date object
    end_day = _as_date(end) if end else _as_date(as_of)

    notes: list[str] = []
    if start:
        return _as_date(start).isoformat(), end_day.isoformat(), notes

    watermark = None if refresh else bar_store.watermark(interval)
    if watermark:
        first = _as_date(calendar.next_trading_day(_as_date(watermark)))
        if first > end_day:
            return None, end_day.isoformat(), [f"湖内已覆盖到 {watermark}，无需入库"]
        notes.append(f"从 {watermark} 之后增量补齐")
        return first.isoformat(), end_day.isoformat(), notes

    sessions = calendar.trading_days(
        (end_day - dt.timedelta(days=DEFAULT_BACKFILL_SESSIONS * 2)).isoformat(),
        end_day.isoformat(),
    )
    window = sessions[-DEFAULT_BACKFILL_SESSIONS:]
    if not window:
        return end_day.isoformat(), end_day.isoformat(), notes
    notes.append(f"冷启动，先补最近 {len(window)} 个交易日")
    return window[0], end_day.isoformat(), notes


def default_universe(
    limit: int | None = None, *, notes: list[str] | None = None
) -> list[str]:
    """Symbols to ingest when the caller did not name any.

    The listed market is the default, *not* the lake's contents: "refill what is
    already there" can never widen coverage, so a lake with three symbols would
    keep three forever.  The listing comes from :mod:`src.data.ingest.universe`
    (cached daily, so it is one cheap call), and the older sources stay as
    fallbacks for the case where no listing feed is reachable.

    ``notes`` (when given) receives one line saying where the list came from —
    "5,576 symbols, the whole listing" and "12 symbols, the ones already in the
    lake" are very different jobs and the report must not blur them.
    """
    from src.data.ingest import universe as universe_module

    symbols = universe_module.list_symbols(notes=notes)
    if not symbols:
        symbols = _lake_symbols()
        origin = "湖内已有标的"
        if not symbols:
            symbols = _board_symbols()
            origin = "板块成分股"
        if notes is not None:
            notes.append(f"标的池：{origin} {len(symbols)} 个（全市场清单不可达，覆盖面不完整）")
    if limit:
        symbols = symbols[: int(limit)]
    return symbols


def _lake_symbols() -> list[str]:
    """Every symbol already present in any dataset — no network."""
    from src.data.lake import dataset_exists, read_dataset

    seen: set[str] = set()
    for name in ("bars_daily", "bars_intraday", "tick_agg", "tradability"):
        try:
            if not dataset_exists(name):
                continue
            frame = read_dataset(name, columns=["symbol"])
        except Exception as exc:  # noqa: BLE001 - a malformed dataset is not fatal here
            logger.debug("universe: reading %s failed (%s)", name, exc)
            continue
        if not frame.empty and "symbol" in frame.columns:
            seen.update(str(s) for s in frame["symbol"].dropna().unique())
    return sorted(seen)


def _board_symbols() -> list[str]:
    """Market breadth from the board taxonomy (cached by market-pick per day)."""
    try:
        from src.market_pick.boards import load_board_universe, unique_symbols

        boards = load_board_universe(dt.date.today())
        return sorted({_canonical(code) for code in unique_symbols(boards) if code})
    except Exception as exc:  # noqa: BLE001 - optional fallback source
        logger.warning("universe: board taxonomy unavailable (%s)", exc)
        return []


# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------


def daily(
    *,
    start: str | None = None,
    end: str | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    refresh: bool = False,
    prefer: str = "auto",
    universe_mode: str = "auto",
    workers: int = _DEFAULT_WORKERS,
    deadline_s: float = 600.0,
    mode: str = "bars",
) -> Any:
    """Ingest daily bars into ``bars_daily``.

    Args:
        start: Inclusive start session; defaults to the lake watermark + 1.
        end: Inclusive end session; defaults to the resolved ``as_of``.
        symbols: Explicit universe; defaults to the whole listed market.
        limit: Cap on symbols (the whole point of a bounded UI job).
        refresh: Re-pull a window the lake already covers.
        prefer: ``"auto"`` (tushare when a token exists, else tencent → sina),
            ``"tushare"``, ``"tencent"``, ``"sina"`` or ``"eastmoney"`` to force
            a path (diagnostics).
        universe_mode: ``"auto"``/``"full"`` — both resolve to the listed
            market; ``"full"`` additionally lifts the session cap, which is what
            a multi-year backfill needs.  Kept as a knob because the UI wants
            the bounded behaviour spelled out in its request.
        workers: Concurrent fetches (clamped to 1…16).
        deadline_s: Soft wall-clock budget; the run stops and says so.
        mode: ``"bars"`` fetches prices; ``"adj_factor"`` is the repair pass
            that fills the null factor column on rows already in the lake (see
            :func:`_repair_adj_factor`).  A repair is a different job — it reads
            nothing from the price sources and its window defaults to what is
            stored, not to a watermark.

    Returns:
        :class:`~src.data.ingest.IngestReport` describing what landed.
    """
    if mode in {"adj_factor", "adj"}:
        return _repair_adj_factor(
            start=start, end=end, symbols=symbols, refresh=refresh, deadline_s=deadline_s
        )
    if mode not in {"bars", ""}:
        raise ValueError(f"未知的 bars_daily mode：{mode!r}（可用 bars / adj_factor）")

    from src.data import calendar
    from src.data.ingest import IngestReport, note, progress

    started = time.monotonic()
    report = IngestReport(dataset="bars_daily")
    window_start, window_end, notes = _resolve_window(
        start, end, interval="1d", refresh=refresh
    )
    report.start, report.end = window_start or window_end, window_end
    report.notes.extend(notes)
    if window_start is None:
        progress(1.0, "已是最新")
        return report

    sessions = calendar.trading_days(window_start, window_end)
    cap = MAX_SESSIONS_FULL if universe_mode == "full" else MAX_SESSIONS_PER_RUN
    if len(sessions) > cap:
        report.notes.append(
            f"区间 {len(sessions)} 个交易日超过单次上限 {cap}，只取最近的一段"
        )
        sessions = sessions[-cap:]
    if not sessions:
        report.notes.append("区间内没有交易日")
        return report

    if symbols:
        universe = [_canonical(s) for s in symbols]
    elif universe_mode == "full":
        from src.data.ingest import universe as universe_module

        universe = universe_module.list_symbols(notes=report.notes)
    else:
        universe = default_universe(limit, notes=report.notes)
    universe = [s for s in dict.fromkeys(universe) if s]
    if limit:
        universe = universe[: int(limit)]

    # Resume: a symbol already holding the end session has the whole range
    # behind it (a bar write upserts a range, not a point), so it is done.
    if universe and not refresh:
        # Compare against the window's first *session*, not its first calendar
        # day: a lake that already starts on 2021-11-01 fully covers a window
        # beginning on Sunday 2021-10-31, and comparing to the calendar date
        # would re-fetch the whole market to prove it.
        universe, skipped_covered = _drop_covered(universe, sessions[0], sessions[-1])
        if skipped_covered:
            report.notes.append(f"{skipped_covered} 个标的已覆盖到 {window_end}，跳过")
    if not universe:
        report.notes.append(f"标的都已覆盖到 {window_end}，无需入库")
        progress(1.0, "已是最新")
        return report

    wanted = set(universe)
    note(
        f"{window_start} → {window_end}（{len(sessions)} 个交易日），"
        f"标的 {len(wanted)}"
    )

    source = ""
    if prefer in {"auto", "tushare"} and _tushare_available():
        source = _ingest_daily_tushare(sessions, wanted, report, started, deadline_s)
    if not source and prefer in {"auto", "tencent", "sina"}:
        chain = (
            ("tencent", "sina")
            if prefer == "auto"
            else (("tencent",) if prefer == "tencent" else ("sina",))
        )
        source = _ingest_daily_per_symbol(
            universe,
            window_start,
            window_end,
            report,
            started,
            deadline_s,
            workers=workers,
            chain=chain,
        )
    if not source and prefer in {"auto", "eastmoney"}:
        source = _ingest_daily_eastmoney(
            universe, window_start, window_end, report, started, deadline_s
        )
    if not source:
        report.notes.append(
            "没有可用的日线来源（未配置 Tushare token，腾讯/东方财富均未返回数据）"
        )
    report.source = source

    report.notes.append(_VOLUME_UNIT_NOTE)
    progress(1.0, "日线入库完成")
    return report


def _drop_covered(
    universe: Sequence[str], start: str, end: str
) -> tuple[list[str], int]:
    """Split *universe* into "still needs work" and "already holds the window".

    A symbol is covered only when its stored span reaches both ends of the
    requested window.  Checking the end alone would be the tempting version and
    the wrong one: a lake seeded by a 30-session cold start also ends at the
    latest session, so a five-year backfill would skip every symbol and report
    success over a month of data.  Anything unexpected (unreadable lake, no
    DuckDB) degrades to "nothing is covered" — a redundant fetch, never a
    silently skipped series.
    """
    from src.data import bars as bar_store
    from src.data import lake

    spans = lake.symbol_spans(bar_store.DATASET_DAILY)
    if not spans:
        return list(universe), 0

    def covered(symbol: str) -> bool:
        first, last = spans.get(symbol, ("", ""))
        return bool(first) and first <= start and last >= end

    todo = [s for s in universe if not covered(s)]
    return todo, len(universe) - len(todo)


def _tushare_available() -> bool:
    try:
        from backtest.loaders.tushare_pool import resolve_tushare_tokens

        return bool(resolve_tushare_tokens())
    except Exception:  # noqa: BLE001 - no config is simply "not available"
        return False


def _ingest_daily_tushare(
    sessions: Sequence[str],
    wanted: set[str],
    report: Any,
    started: float,
    deadline_s: float,
) -> str:
    """Fetch one call per session for the whole market. Returns the source name."""
    from src.data import bars as bar_store
    from src.data.ingest import cancelled, checkpoint, note, progress

    try:
        from backtest.loaders.tushare_pool import (
            TushareAllTokensBlocked,
            TushareQuotaExhausted,
            get_tushare_pool,
        )
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"Tushare 不可用：{exc}")
        return ""

    pool = get_tushare_pool()
    written = 0
    factor_available = True

    for index, session in enumerate(sessions):
        if cancelled():
            report.notes.append(f"已取消，已完成 {index}/{len(sessions)} 个交易日")
            break
        if _elapsed(started, deadline_s):
            report.notes.append(f"达到单次时限，已完成 {index}/{len(sessions)} 个交易日")
            break
        progress(index / max(len(sessions), 1), f"日线 {session}")
        stamp = session.replace("-", "")
        try:
            raw = pool.call("daily", trade_date=stamp)
        except (TushareQuotaExhausted, TushareAllTokensBlocked) as exc:
            report.notes.append(f"{session} 取数受限：{exc}")
            break
        except Exception as exc:  # noqa: BLE001 - one bad session must not abort the run
            report.notes.append(f"{session} 取数失败：{exc}")
            continue
        if raw is None or raw.empty:
            report.notes.append(f"{session} 无行情（可能非交易日或尚未发布）")
            continue

        frame = _normalize_tushare_daily(raw, wanted)
        if frame.empty:
            continue
        if factor_available:
            factor_available = _attach_adj_factor(pool, frame, stamp, report)
        rows = bar_store.save_long_bars(frame, "1d", source="tushare")
        written += rows
        report.rows += rows
        report.sessions += 1
        report.symbols = max(report.symbols, int(frame["symbol"].nunique()))
        checkpoint(
            {"session": session, "done": index + 1, "total": len(sessions)},
            done=index + 1,
            total=len(sessions),
        )

    if written:
        note(f"Tushare 写入 {written} 行")
        return "tushare"
    return ""


def _normalize_tushare_daily(raw: pd.DataFrame, wanted: set[str]) -> pd.DataFrame:
    """Map Tushare ``daily`` columns onto the lake contract.

    ``vol`` is 手 and ``amount`` is 千元 — the ×1000 is the only unit change, and
    it happens here so nothing downstream has to remember it.
    """
    frame = raw.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    if "ts_code" not in frame.columns:
        return pd.DataFrame()
    frame = frame.rename(columns={"ts_code": "symbol", "trade_date": "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    if wanted:
        frame = frame[frame["symbol"].astype(str).isin(wanted)]
    if frame.empty:
        return frame
    frame["volume"] = pd.to_numeric(frame.get("vol"), errors="coerce")
    frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce") * 1000.0
    frame["pre_close"] = pd.to_numeric(frame.get("pre_close"), errors="coerce")
    frame["pct_chg"] = pd.to_numeric(frame.get("pct_chg"), errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str)
    # Declared-but-unavailable: a null column keeps the contract stable, whereas
    # dropping it makes the console report schema drift on every daily run.
    if "adj_factor" not in frame.columns:
        frame["adj_factor"] = float("nan")
    keep = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pre_close",
        "pct_chg",
        "adj_factor",
    ]
    return frame[[c for c in keep if c in frame.columns]]


def _attach_adj_factor(pool: Any, frame: pd.DataFrame, stamp: str, report: Any) -> bool:
    """Add ``adj_factor`` for the session; ``False`` when the plan forbids it.

    A missing factor is a note, not a failure: unadjusted bars without a factor
    are still correct for everything but adjusted-return work, and the registry
    report shows the column as null rather than inventing 1.0.

    On a standard Tushare plan this endpoint is capped at **one call per hour**
    (measured, error 40203), so in practice the column is filled by the
    ``mode="adj_factor"`` repair rather than here.  The call stays because a plan
    with a real quota makes it the cheapest path — one request per session.
    """
    try:
        factors = pool.call("adj_factor", trade_date=stamp)
    except Exception as exc:  # noqa: BLE001 - permission/quota/quota-plan dependent
        report.notes.append(f"adj_factor 不可用，复权因子留空（{exc}）")
        return False
    if factors is None or factors.empty or "ts_code" not in factors.columns:
        return False
    lookup = dict(zip(factors["ts_code"].astype(str), factors["adj_factor"]))
    frame["adj_factor"] = frame["symbol"].map(lookup).astype(float)
    return True


def _repair_adj_factor(
    *,
    start: str | None = None,
    end: str | None = None,
    symbols: Sequence[str] | None = None,
    refresh: bool = False,
    deadline_s: float = 1800.0,
) -> Any:
    """Fill ``bars_daily.adj_factor`` on rows that are already stored.

    This is a repair, not an ingest: no price is fetched, no bar is created or
    deleted, and a symbol no source knows keeps its null.  The window defaults to
    what the lake holds rather than to a watermark, because the question here is
    "which stored rows still have no factor" — one aggregate over the partitions,
    not a scan.

    The factor is cumulative and monotonic, which fixes the arithmetic downstream
    (and in ``docs/design/data-center.md``):

    * 后复权价 = ``close * adj_factor``
    * 前复权价 = ``close * adj_factor / adj_factor_latest``

    Source: baostock's ``backAdjustFactor``, one call per symbol, whole history.
    Tushare's ``adj_factor`` has the cheaper shape (one call per session, whole
    market) but a standard plan caps that endpoint at **one call per hour**,
    which turns a five-year repair into a two-month job.  That was measured
    before this loop was replaced, not assumed.

    Progress is checkpointed every :data:`_ADJ_CHUNK_SYMBOLS` symbols, so a run
    that hits its deadline is continued — not restarted — by running it again.
    """
    from src.data import lake
    from src.data.ingest import IngestReport, cancelled, checkpoint, note, progress

    started = time.monotonic()
    report = IngestReport(dataset="bars_daily")
    try:
        import baostock  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - optional dependency
        report.notes.append(f"未安装 baostock，无法计算复权因子：{exc}")
        return report

    pending, skipped, unsupported, window, session_count = _symbols_missing_factor(
        start, end, refresh=refresh, report=report
    )
    if symbols:
        wanted = {str(s).strip().upper() for s in symbols if str(s).strip()}
        pending = [s for s in pending if s in wanted]
        report.notes.append(f"只修 {len(pending)} 个指定标的")
    if unsupported and not symbols:
        report.notes.append(
            f"{unsupported} 个标的没有复权因子来源（baostock 不认北交所代码），保持空值"
        )
    report.skipped = skipped
    report.sessions = session_count
    report.start, report.end = window
    if not pending:
        report.notes.append(
            "指定标的没有待修复的缺口" if symbols else "存储的日线都已有复权因子，无需修复"
        )
        progress(1.0, "复权因子已覆盖")
        return report

    years = range(int(window[0][:4]), int(window[1][:4]) + 1)
    source = _BaostockFactors()
    filled = 0
    done_symbols = 0
    stopped = False
    regressed: list[str] = []

    try:
        for offset in range(0, len(pending), _ADJ_CHUNK_SYMBOLS):
            chunk = pending[offset : offset + _ADJ_CHUNK_SYMBOLS]
            if cancelled():
                report.notes.append(
                    f"已取消，已完成 {done_symbols}/{len(pending)} 个标的"
                )
                stopped = True
                break
            if _elapsed(started, deadline_s):
                report.notes.append(
                    f"达到单次时限，已完成 {done_symbols}/{len(pending)} 个标的"
                )
                stopped = True
                break
            progress(offset / len(pending), f"复权因子 {chunk[0]}")
            events = source.events(chunk, report)
            if not events:
                continue
            for year in years:
                frame = _read_symbol_slice(year, list(events), window)
                if frame.empty:
                    continue
                stepped = _apply_factor_steps(frame, events)
                filled += int(len(stepped))
                regressed.extend(_non_monotonic_symbols(stepped))
                lake.write_dataset(stepped, "bars_daily")
            done_symbols += len(chunk)
            # Written after the partitions: the cursor promises durability.
            checkpoint(
                {"done": done_symbols, "total": len(pending), "after": chunk[-1]},
                done=done_symbols,
                total=len(pending),
            )
    finally:
        source.close()

    report.rows = filled
    report.symbols = done_symbols
    report.source = "baostock" if filled else ""
    if filled:
        note(f"复权因子填入 {filled} 行 / {done_symbols} 个标的")
        report.notes.append(
            "复权口径：后复权价 = close × adj_factor，前复权价 = close × adj_factor ÷ 最新一期因子；"
            "价格与 source 未改动，因子来源为 baostock backAdjustFactor"
        )
    if source.empty_symbols:
        report.notes.append(
            f"{source.empty_symbols} 个标的 baostock 无除权记录，按因子 1.0 写入（窗口内无分红送转）"
        )
    if source.failed:
        report.notes.append(f"{source.failed} 个标的取数失败，重跑会从缺口继续")
    if regressed:
        report.notes.append(
            f"{len(set(regressed))} 个标的的因子在某一天回退（源站阶梯不单调，多为源站误差；"
            "缩股才会合法回退）：复权收益在那一天会偏几个百分点"
        )
    progress(1.0, "复权因子修复完成" if not stopped else "复权因子修复中断")
    return report


def _symbols_missing_factor(
    start: str | None,
    end: str | None,
    *,
    refresh: bool,
    report: Any,
) -> tuple[list[str], int, int, tuple[str, str], int]:
    """Symbols with at least one stored row lacking a factor, plus counts.

    A symbol counts as done only when *every* stored row for it has a factor.  A
    percentage tolerance would be tempting and wrong: the missing rows are
    exactly the ones whose adjusted return would silently be computed as if no
    dividend ever happened, and a 99% threshold hides them.

    Returns ``(pending, skipped, unsupported, window, sessions)`` where
    ``unsupported`` is the subset baostock cannot answer for (Northbound/BSE
    codes it rejects), and ``sessions`` is how many distinct sessions the window
    holds — reported so the console can say "5,989,248 行 over 1,212 sessions".
    """
    from src.data import lake

    low, high, rows = lake.dataset_bounds("bars_daily")
    if not rows:
        report.notes.append("湖内没有日线数据，先跑一次 bars_daily 行情入库")
        return [], 0, 0, ("", ""), 0
    window = (start or low or "", end or high or "")
    where = (
        'CAST("date" AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)',
        [window[0], window[1]],
    )
    coverage = lake.aggregate_dataset(
        "bars_daily",
        'symbol, count(*) AS bars, count("adj_factor") AS covered',
        group_by="1",
        where=where,
    )
    if coverage.empty:
        report.notes.append(f"{window[0]} → {window[1]} 区间湖内没有日线")
        return [], 0, 0, window, 0

    total = lake.aggregate_dataset(
        "bars_daily",
        'count(DISTINCT CAST("date" AS DATE)) AS sessions',
        where=where,
    )
    sessions = int(total["sessions"].iloc[0]) if not total.empty else 0

    symbols = coverage["symbol"].astype(str)
    if refresh:
        pending = symbols.tolist()
        skipped = 0
    else:
        needs = coverage["covered"].astype(float) < coverage["bars"].astype(float)
        pending = symbols[needs].tolist()
        skipped = int(len(coverage) - len(pending))
    supported = [s for s in pending if _bs_code(s) is not None]
    unsupported = len(pending) - len(supported)
    return sorted(supported), skipped, unsupported, window, sessions


def _bs_code(symbol: str) -> str | None:
    """Map ``600519.SH`` to baostock's ``sh.600519``; ``None`` when unsupported.

    Beijing (``.BJ``) is rejected outright by baostock — it answers
    ``股票代码未标识sh或sz`` — so those symbols are counted and left null rather
    than given a made-up factor of 1.0.
    """
    text = str(symbol).strip().upper()
    code, _, market = text.partition(".")
    if not code.isdigit() or market not in {"SH", "SZ"}:
        return None
    return f"{market.lower()}.{code}"


def _factor_steps(dates: pd.Series, events: pd.DataFrame) -> pd.Series:
    """Factor valid on each session: the latest ex-dividend event on or before it.

    The factor is a step function, not a per-session series — a symbol pays a
    dividend a few times a year and nothing happens in between.  Verified against
    the lake on 600519.SH: applying the event's factor *from* its 除权日 keeps the
    adjusted series continuous (the 2026-06-26 raw -3.59% becomes -1.30%, the
    dividend removed) while applying it only afterwards leaves a jump.

    Before the first recorded event the factor is 1.0: ``backAdjustFactor`` is
    cumulative from listing, so "no event yet" is the unadjusted baseline.
    """
    if events is None or events.empty:
        return pd.Series(1.0, index=dates.index, dtype="float64")
    ordered = events.sort_values("date")
    position = np.searchsorted(ordered["date"].values, dates.values, side="right")
    values = ordered["factor"].to_numpy(dtype="float64")
    taken = np.where(position > 0, values[np.clip(position - 1, 0, len(values) - 1)], 1.0)
    return pd.Series(taken, index=dates.index, dtype="float64")


def _read_symbol_slice(
    year: int, symbols: Sequence[str], window: tuple[str, str]
) -> pd.DataFrame:
    """Stored bars for *symbols* in one year partition, within the window.

    The write path is an upsert of whole rows, so the slice has to be read with
    its OHLC intact — writing a three-column frame would replace prices with
    nulls.
    """
    from src.data import lake

    placeholders = ", ".join("?" for _ in symbols)
    frame = lake.read_dataset(
        "bars_daily",
        filters={"year": year},
        where=(
            f'"symbol" IN ({placeholders}) AND CAST("date" AS DATE) '
            "BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
            [*symbols, window[0], window[1]],
        ),
    )
    if frame.empty:
        return frame
    # ``year`` is a hive artifact restored by read_dataset; persisting it would
    # add a redundant payload column the registry does not declare.
    return frame.drop(columns=["year"], errors="ignore")


def _non_monotonic_symbols(frame: pd.DataFrame) -> list[str]:
    """Symbols whose factor steps *down* inside this slice.

    A cumulative factor only rises for a dividend.  It can legitimately fall for
    a share consolidation, and that comes with a matching jump up in the price —
    so a bare decline fabricates a loss on that day.  baostock carries at least
    one (000002.SZ 2025-01-09: factor -1.18%, close -0.14%).  The repair must not
    rewrite a vendor series on its own judgment, but it must report that the
    number is odd instead of hiding it behind 6M rows.
    """
    if frame.empty or "adj_factor" not in frame.columns:
        return []
    ordered = frame.sort_values(["symbol", "date"], kind="stable")
    step = ordered.groupby("symbol")["adj_factor"].diff()
    return sorted(set(ordered.loc[step < -1e-12, "symbol"].astype(str)))


def _apply_factor_steps(frame: pd.DataFrame, events: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Replace ``adj_factor`` per symbol with its step function.

    Symbols that failed to fetch are absent from *events* and keep whatever they
    had: a network error must not blank a factor that was already correct.
    """
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"]).dt.normalize()
    work["symbol"] = work["symbol"].astype(str)
    pieces = []
    for symbol, part in work.groupby("symbol", sort=False):
        part = part.copy()
        part["adj_factor"] = _factor_steps(part["date"], events.get(str(symbol)))
        pieces.append(part)
    if not pieces:
        return work
    return pd.concat(pieces, ignore_index=True)


@contextlib.contextmanager
def _quiet_stdout():
    """Swallow a library's banner without losing our own output.

    baostock writes ``login success!`` straight to stdout; that is harmless in a
    terminal and fatal to ``scripts/ingest.py --json``, which promises a single
    JSON document there.  It also prints its own Chinese error when a read
    fails, and a failed read is exactly what the budget guard provokes — so the
    window covers one login/logout *or* one guarded query.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


# One request can legitimately take 90 seconds (a symbol, five years of
# 5-minute bars), so this is not a "slow response" guard — it is a "the socket
# is dead" guard.  baostock's reader loops until it sees a 13-byte terminator and
# never tests for a closed pipe: a dropped peer makes ``recv`` return ``b""``
# forever and the loop spins at 100% CPU, *while the job's heartbeat thread stays
# perfectly healthy*.  Nothing above this layer can tell that apart from honest
# work, which is why the abort has to live down here.
QUERY_BUDGET_S = 180.0


class BaostockTimeout(RuntimeError):
    """A baostock round-trip outlived its budget and was aborted."""


def _default_socket() -> Any:
    """The module-level socket baostock keeps on ``baostock.common.context``."""
    try:
        import baostock.common.context as context

        return getattr(context, "default_socket", None)
    except Exception:  # noqa: BLE001 - absence just means "nothing to drop"
        return None


def _drop_socket(sock: Any) -> None:
    """Force a blocked ``recv`` to return: ``shutdown`` first, then ``close``.

    ``shutdown`` is the part that actually wakes the reader — closing alone
    leaves a pending ``recv`` blocked on some platforms — and every error is
    swallowed because the socket is already suspect by the time we get here.
    """
    if sock is None:
        return
    import socket as socket_module

    try:
        sock.shutdown(socket_module.SHUT_RDWR)
    except Exception:  # noqa: BLE001 - already shut down, or never connected
        pass
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


class _BaostockSession:
    """One baostock socket: login once per run, re-login when the link drops.

    baostock keeps a module-level socket (``baostock.data.resultset``), so two
    classes both talking to it must not each own a login — that is why the
    transport lives here and the endpoints live in the subclasses.  A dropped
    ``recv`` is reported by baostock as ``10002007``; it is routine on a long
    backfill and costs one symbol, never the run.
    """

    def __init__(self) -> None:
        self.failed = 0
        self._connected = False

    def _guarded(self, call: Any, *, budget: float | None = None) -> Any:
        """Run one socket round-trip, aborting it if it outlives *budget*.

        The watchdog drops the socket rather than raising inside ``call``: the
        only thread that can safely interrupt a blocked ``recv`` is one that
        does not own it.  Dropping it mid-read makes baostock report
        ``error_code 0`` with a **truncated** page, so a tripped guard must
        raise — returning that frame would write half a year of bars to the lake
        and call it success.
        """
        limit = QUERY_BUDGET_S if budget is None else budget
        done = threading.Event()
        tripped: list[bool] = []

        def watchdog() -> None:
            if not done.wait(limit):
                tripped.append(True)
                _drop_socket(_default_socket())

        guard = threading.Thread(target=watchdog, name="baostock-budget", daemon=True)
        guard.start()
        try:
            # The dropped socket makes baostock print its own error to stdout,
            # which would corrupt the single JSON document ``ingest.py --json``
            # promises there.  Swallow the window; the exception carries the
            # same information.
            with _quiet_stdout():
                value = call()
        except Exception as exc:
            # A trip almost never surfaces as our exception: the dropped socket
            # makes baostock raise ``10002007`` first, so the flag has to be
            # consulted here too or the connection stays "connected" while every
            # later call fails instantly against the dead socket.
            if tripped:
                self._connected = False
                raise BaostockTimeout(
                    f"baostock 单次请求超过 {limit:g}s，已断开重连"
                ) from exc
            raise
        finally:
            done.set()
            guard.join(timeout=1.0)
        if tripped:
            # The connection is unusable now; the caller's ``_reconnect`` would
            # otherwise "succeed" against a dead socket.
            self._connected = False
            raise BaostockTimeout(f"baostock 单次请求超过 {limit:g}s，已断开重连")
        return value

    # -- session ----------------------------------------------------------
    def _login(self) -> None:
        import baostock as bs

        # baostock prints "login success!" to stdout, which would corrupt the
        # ``--json`` document the CLI promises on stdout.  The window is one call.
        with _quiet_stdout():
            result = bs.login()
        if str(getattr(result, "error_code", "")) != "0":
            raise RuntimeError(f"baostock 登录失败：{getattr(result, 'error_msg', '')}")
        self._connected = True

    def close(self) -> None:
        if not self._connected:
            return
        try:
            import baostock as bs

            with _quiet_stdout():
                bs.logout()
        except Exception:  # noqa: BLE001 - logout failure is not worth a report
            logger.debug("baostock logout failed", exc_info=True)
        self._connected = False

    def _reconnect(self) -> None:
        self.close()
        try:
            self._login()
        except Exception as exc:  # noqa: BLE001 - caller retries on the next chunk
            logger.debug("baostock re-login failed: %s", exc)

    def _ensure(self) -> None:
        if not self._connected:
            self._login()

    @staticmethod
    def _rows(answer: Any) -> list[list[str]]:
        """Drain a baostock result set, raising on a failed page."""
        if str(getattr(answer, "error_code", "")) != "0":
            raise RuntimeError(
                f"{getattr(answer, 'error_code', '')} {getattr(answer, 'error_msg', '')}"
            )
        rows = []
        while answer.next():
            rows.append(answer.get_row_data())
        return rows


class _BaostockFactors(_BaostockSession):
    """Per-symbol 复权因子 over baostock's socket protocol.

    One login per run, re-login on a dropped connection, and a failure is a note
    rather than an abort: the repair is idempotent, so the next run retries the
    symbol that failed instead of the whole market.
    """

    def __init__(self) -> None:
        super().__init__()
        self.empty_symbols = 0

    # -- data -------------------------------------------------------------
    def events(self, symbols: Sequence[str], report: Any) -> dict[str, pd.DataFrame]:
        """Symbol → its ex-dividend events, oldest first (empty frame if none)."""
        self._ensure()
        out: dict[str, pd.DataFrame] = {}
        failures = 0
        for symbol in symbols:
            code = _bs_code(symbol)
            if code is None:
                continue
            try:
                frame = self._guarded(lambda: self._query(code))
            except Exception as exc:  # noqa: BLE001 - socket/format drift
                failures += 1
                self.failed += 1
                if failures <= 2:
                    report.notes.append(f"{symbol} 复权因子取数失败：{exc}")
                self._reconnect()
                continue
            out[symbol] = frame
            if frame.empty:
                self.empty_symbols += 1
        return out

    def _query(self, code: str) -> pd.DataFrame:
        import baostock as bs

        answer = bs.query_adjust_factor(
            code=code, start_date="1990-01-01", end_date=dt.date.today().isoformat()
        )
        rows = self._rows(answer)
        if not rows:
            return pd.DataFrame(columns=["date", "factor"])
        frame = pd.DataFrame(rows, columns=answer.fields)
        frame = frame[["dividOperateDate", "backAdjustFactor"]].copy()
        frame["date"] = pd.to_datetime(frame["dividOperateDate"], errors="coerce")
        frame["factor"] = pd.to_numeric(frame["backAdjustFactor"], errors="coerce")
        return frame.dropna(subset=["date", "factor"]).sort_values("date")[["date", "factor"]]


class _BaostockMinutes(_BaostockSession):
    """Minute bars from baostock, one call per symbol per year.

    baostock is the only free archive with minute history at all: Tencent and
    Sina return a *window* (32 days for Eastmoney's 5-minute feed, 320 bars for
    Tencent's ``mkline``), and Tushare's ``stk_mins`` is capped at one call per
    minute on a standard plan.  baostock answers back to 2019-01-02 — measured
    here — with ``date``, ``time``, OHLC, ``volume`` and ``amount``.

    Two unit facts are fixed on the way in, because baostock disagrees with the
    rest of the pipeline:

    * ``volume`` arrives in **shares**; the lake stores 手, so it is divided by
      100.  A 5-minute bar of 600519 at 226,869 is 2,268 lots, not 226,869.
    * ``amount`` is already 元, which is what the registry declares.
    """

    def __init__(self) -> None:
        super().__init__()
        self.empty = 0

    def bars(self, symbol: str, start: str, end: str, freq: str) -> pd.DataFrame:
        """One symbol's bars in ``[start, end]``; empty frame when it has none."""
        code = _bs_code(symbol)
        if code is None:
            return pd.DataFrame()
        self._ensure()
        frame = self._guarded(lambda: self._query(code, start, end, freq))
        if frame.empty:
            self.empty += 1
        return frame

    def _query(self, code: str, start: str, end: str, freq: str) -> pd.DataFrame:
        import baostock as bs

        answer = bs.query_history_k_data_plus(
            code,
            "date,time,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency=freq,
            adjustflag="3",  # 不复权: the lake stores what the exchange printed
        )
        rows = self._rows(answer)
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=answer.fields)
        stamp = frame["time"].astype(str).str.slice(0, 14)
        index = pd.to_datetime(stamp, format="%Y%m%d%H%M%S", errors="coerce")
        # ``.to_numpy()`` is not cosmetic: handing a Series into ``DataFrame(...,
        # index=<datetime>)`` makes pandas *re-align* it by label, and every
        # value comes back NaN.  passthrough alignment is the whole point here.
        out = pd.DataFrame(
            {
                "open": pd.to_numeric(frame["open"], errors="coerce").to_numpy(),
                "high": pd.to_numeric(frame["high"], errors="coerce").to_numpy(),
                "low": pd.to_numeric(frame["low"], errors="coerce").to_numpy(),
                "close": pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
                "volume": (
                    pd.to_numeric(frame["volume"], errors="coerce") / 100.0
                ).to_numpy(),
                "amount": pd.to_numeric(frame["amount"], errors="coerce").to_numpy(),
            }
        )
        out.index = pd.DatetimeIndex(index, name="date")
        return out[out.index.notna()]



_TENCENT_DAILY_HOSTS = ("https://web.ifzq.gtimg.cn", "https://ifzq.gtimg.cn")
_TENCENT_DAILY_PATH = "/appstock/app/fqkline/get"
_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://web.ifzq.gtimg.cn/",
}
# The endpoint accepts a date range *and* a count; 2000 covers five years of
# sessions in one request, which is what makes a per-symbol backfill viable.
_TENCENT_MAX_BARS = 2000


def _tencent_code(symbol: str) -> str | None:
    """Map ``600519.SH`` to the ``sh600519`` form the kline endpoint wants."""
    text = str(symbol).strip().upper()
    if "." not in text:
        return None
    code, _, market = text.partition(".")
    if not code.isdigit() or market not in {"SH", "SZ"}:
        return None  # Beijing has no kline feed on this host
    return f"{market.lower()}{code}"


def _fetch_tencent_raw_daily(
    symbol: str,
    start: str,
    end: str,
    *,
    count: int = _TENCENT_MAX_BARS,
    strict: bool = False,
) -> list[list[str]]:
    """Raw (不复权) daily rows from Tencent, newest last.

    The ``fq`` slot is left empty on purpose: ``qfq`` returns forward-adjusted
    prices under a ``qfqday`` key, which would look identical to the untrained
    eye and be wrong for every band/backtest consumer.

    ``strict`` re-raises when every host fails instead of returning ``[]``.  A
    backfill needs that difference: "this symbol has no bars" (delisted, newly
    listed, suspended) must not be retried, while "the network hiccuped" must.
    """
    import json
    import urllib.request

    code = _tencent_code(symbol)
    if not code:
        return []
    query = f"param={code},day,{start},{end},{count},"
    last_error: Exception | None = None
    for host in _TENCENT_DAILY_HOSTS:
        try:
            request = urllib.request.Request(
                f"{host}{_TENCENT_DAILY_PATH}?{query}", headers=_TENCENT_HEADERS
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - one host may resolve, the other not
            last_error = exc
            continue
        block = (payload.get("data") or {}).get(code) or {}
        rows = block.get("day") or []
        if rows:
            return rows
    if last_error is not None:
        logger.debug("tencent raw daily failed for %s: %s", symbol, last_error)
        if strict:
            raise last_error
    return []


def _fetch_tencent_frame(
    symbol: str, start: str, end: str, *, attempts: int = 3
) -> pd.DataFrame:
    """One symbol's normalised frame, retried on transport errors only."""
    delay = 0.3
    for attempt in range(attempts):
        try:
            rows = _fetch_tencent_raw_daily(symbol, start, end, strict=True)
        except Exception as exc:  # noqa: BLE001 - retry, then give up on this symbol
            if attempt + 1 >= attempts:
                logger.debug("giving up on %s after %d attempts (%s)", symbol, attempts, exc)
                return pd.DataFrame()
            time.sleep(delay)
            delay *= 2
            continue
        return _normalize_tencent_daily(rows, symbol) if rows else pd.DataFrame()
    return pd.DataFrame()


# --- Sina fallback ---------------------------------------------------------
#
# Tencent is the primary source (a full date range in one request, 2,000 bars),
# but its edge rejects bursts with HTTP 501 once a backfill gets going, and a
# blocked source that keeps being asked is just a slower failure.  Sina serves
# the same *unadjusted* daily series from a different host — verified bar for bar
# against the lake (close identical, volume identical after /100) — with two
# limits worth knowing: at most 1,500 bars per request (≈6 years, enough for the
# retention window) and no turnover, so ``amount`` stays null there too.

_SINA_HOST = "https://quotes.sina.cn"
_SINA_PATH = "/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
_SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
# 1,500 is the largest ``datalen`` the endpoint honours; 3,000 returns null.
_SINA_MAX_BARS = 1500

# Edge/proxy rejections mean "stop asking", not "this symbol is empty".
_BLOCKED_CODES = {403, 418, 429, 451, 500, 501, 502, 503, 504}


def _is_blocked(exc: BaseException) -> bool:
    """True when *exc* looks like the source refusing the caller, not the data."""
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in _BLOCKED_CODES
    return False


class _Pacer:
    """Space out request starts so a backfill is a stream, not a burst.

    Concurrency alone decides how many requests are in flight; it does not stop
    5,000 of them from leaving in the first ten seconds.  This caps the *start*
    rate across all worker threads, which is what the edge actually measures.
    """

    def __init__(self, per_second: float = _REQUEST_PER_SECOND) -> None:
        self.interval = 1.0 / max(float(per_second), 0.5)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


_pacer = _Pacer()


def _sina_code(symbol: str) -> str | None:
    """Map ``600519.SH`` to ``sh600519`` (Beijing included: Sina serves it)."""
    text = str(symbol).strip().upper()
    code, _, market = text.partition(".")
    if not code.isdigit() or market not in {"SH", "SZ", "BJ"}:
        return None
    return f"{market.lower()}{code}"


def _fetch_sina_raw_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """Raw unadjusted daily rows from Sina, oldest first.

    The endpoint takes a bar *count*, not a date range, so the window is applied
    locally.  It is the fallback precisely because of that: it cannot express
    "from 2021-10-31", only "the last 1,500 bars".
    """
    import json
    import urllib.request

    code = _sina_code(symbol)
    if not code:
        return []
    url = (
        f"{_SINA_HOST}{_SINA_PATH}?symbol={code}&scale=240&ma=no"
        f"&datalen={_SINA_MAX_BARS}"
    )
    _pacer.wait()
    request = urllib.request.Request(url, headers=_SINA_HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, list) else []


def _normalize_sina_daily(
    rows: Sequence[dict[str, Any]], symbol: str, start: str, end: str
) -> pd.DataFrame:
    """Map Sina rows onto the lake contract, clipping them to the window.

    ``volume`` is 股 here (Sina), 手 everywhere else in the lake: the /100 is
    done once, at the boundary, exactly like the Tushare 千元→元 conversion.
    """
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(list(rows))
    if "day" not in frame.columns:
        return pd.DataFrame()
    frame = frame.rename(columns={"day": "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date", kind="stable")
    frame = frame[
        (frame["date"] >= pd.Timestamp(start))
        & (frame["date"] < pd.Timestamp(end) + pd.Timedelta(days=1))
    ]
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["volume"] = frame["volume"] / 100.0
    # A bar with no range is not a bar.  Sina occasionally returns a session with
    # ``open/high/low = 0`` and only a settlement close (three STAR-market names
    # on 2024-11-06); keeping those rows poisons every band and high/low read and
    # shows up as a permanent quality alert over three rows in six million.  The
    # session itself is not invented or filled — it is absent, and the coverage
    # matrix shows the gap.
    frame = frame[(frame["high"] > 0) & (frame["low"] > 0)]
    if frame.empty:
        return frame
    frame["symbol"] = symbol
    frame["pre_close"] = frame["close"].shift(1)
    frame["pct_chg"] = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
    frame["amount"] = float("nan")
    frame["adj_factor"] = float("nan")
    keep = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pre_close",
        "pct_chg",
        "adj_factor",
    ]
    return frame[[c for c in keep if c in frame.columns]].reset_index(drop=True)


def _fetch_sina_frame(symbol: str, start: str, end: str) -> pd.DataFrame:
    try:
        rows = _fetch_sina_raw_daily(symbol, start, end)
    except Exception as exc:  # noqa: BLE001 - the fallback may be blocked too
        logger.debug("sina daily failed for %s: %s", symbol, exc)
        return pd.DataFrame()
    return _normalize_sina_daily(rows, symbol, start, end)


def _fetch_daily_frame(
    symbol: str, start: str, end: str, *, chain: Sequence[str], state: dict[str, Any]
) -> tuple[pd.DataFrame, str]:
    """Fetch one symbol through the source chain; returns ``(frame, source)``.

    A blocked primary is remembered in ``state`` so the remaining 5,000 symbols
    go straight to the fallback instead of each paying for a rejection.
    """
    for source in chain:
        if source == "tencent":
            if state.get("tencent_blocked"):
                continue
            for attempt in range(2):
                try:
                    _pacer.wait()
                    rows = _fetch_tencent_raw_daily(symbol, start, end, strict=True)
                except Exception as exc:  # noqa: BLE001 - classify, then decide
                    if _is_blocked(exc):
                        state["tencent_blocked"] = True
                        state["blocked"] = int(state.get("blocked") or 0) + 1
                        logger.warning("tencent daily blocked (%s); falling back", exc)
                        break
                    if attempt == 1:
                        break
                    time.sleep(0.3)
                    continue
                if rows:
                    return _normalize_tencent_daily(rows, symbol), "tencent"
                break
        elif source == "sina":
            frame = _fetch_sina_frame(symbol, start, end)
            if not frame.empty:
                return frame, "sina"
    return pd.DataFrame(), ""



class _BarSink:
    """Buffer bars per year and write each partition as few times as possible.

    ``lake.write_dataset`` is read-modify-write per partition.  Writing symbol by
    symbol therefore re-reads and rewrites the same year file once per symbol —
    for a 5,500-symbol, five-year backfill that is quadratic IO and the reason a
    naive loop never finishes.  Buffering to a row threshold turns ~27,000 file
    rewrites into a few dozen.

    Flushing is per year, not per batch, because ``write_dataset`` partitions on
    the year anyway: draining one year's buffer rewrites exactly one file.
    Buckets are keyed by ``(year, source)`` so a run that changes source halfway
    through still writes honest provenance — rows fetched from Sina must not be
    labelled Tencent just because they share a partition.
    """

    def __init__(self, interval: str, *, flush_rows: int = _FLUSH_ROWS) -> None:
        self.interval = interval
        self.flush_rows = max(int(flush_rows), 1)
        self.written = 0
        self.rows_by_source: dict[str, int] = {}
        self._buckets: dict[tuple[int, str], list[pd.DataFrame]] = {}
        self._pending: dict[tuple[int, str], int] = {}

    def add(self, frame: pd.DataFrame, source: str = "") -> None:
        if frame is None or frame.empty:
            return
        label = source or "unknown"
        years = pd.to_datetime(frame["date"], errors="coerce").dt.year
        for year, chunk in frame.groupby(years, sort=False):
            if pd.isna(year):
                continue
            key = (int(year), label)
            self._buckets.setdefault(key, []).append(chunk)
            self._pending[key] = self._pending.get(key, 0) + len(chunk)

    def flush(self, *, force: bool = False) -> int:
        from src.data import bars as bar_store

        written = 0
        for key in sorted(self._buckets):
            size = self._pending.get(key, 0)
            if not size:
                self._buckets.pop(key, None)
                continue
            if not force and size < self.flush_rows:
                continue
            parts = self._buckets.pop(key, [])
            self._pending.pop(key, None)
            if not parts:
                continue
            year, label = key
            frame = pd.concat(parts, ignore_index=True)
            wrote = bar_store.save_long_bars(frame, self.interval, source=label)
            written += wrote
            self.rows_by_source[label] = self.rows_by_source.get(label, 0) + wrote
        self.written += written
        return written


def _ingest_daily_per_symbol(
    universe: Sequence[str],
    start: str,
    end: str,
    report: Any,
    started: float,
    deadline_s: float,
    *,
    workers: int = _DEFAULT_WORKERS,
    chain: Sequence[str] = ("tencent", "sina"),
) -> str:
    """Per-symbol daily backfill over a source chain. Returns the sources used.

    Runs a small thread pool over the universe with a bounded in-flight window:
    enough concurrency to make 5,500 symbols minutes instead of hours, and a cap
    on outstanding futures so a fast feed cannot outrun the writer and pile up
    every frame in memory.  Request *starts* are additionally paced (see
    :class:`_Pacer`): a burst is what gets the source rejected, and once the
    primary is blocked the chain switches to the fallback for the rest of the run
    instead of paying for 5,000 rejections.

    On deadline the loop stops *submitting*, drains what is already in flight,
    flushes the sink and reports ``已完成 x/y`` — a bounded job that leaves a
    consistent lake and a resumable remainder, never a half-written partition.
    A cancel request is handled the same way: the in-flight window drains so the
    partition is consistent, and the remainder is picked up next run because
    :func:`_drop_covered` skips what already landed.
    """
    from src.data.ingest import cancelled, checkpoint, note, progress

    if not universe:
        report.notes.append("没有可用标的池，请显式传入 symbols")
        return ""

    total = len(universe)
    workers = max(1, min(int(workers or 1), _MAX_WORKERS))
    sink = _BarSink("1d")
    state: dict[str, Any] = {}
    written_symbols = 0
    empty_symbols = 0
    in_flight: dict[Any, str] = {}
    iterator = iter(universe)
    exhausted = False

    def submit_next(pool: ThreadPoolExecutor) -> bool:
        nonlocal exhausted
        if exhausted:
            return False
        symbol = next(iterator, None)
        if symbol is None:
            exhausted = True
            return False
        in_flight[
            pool.submit(_fetch_daily_frame, symbol, start, end, chain=chain, state=state)
        ] = symbol
        return True

    window = max(workers * 3, 8)
    tick = time.monotonic()
    done = 0
    called_off = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ingest-daily") as pool:
        for _ in range(window):
            if not submit_next(pool):
                break

        while in_flight:
            # Checked every poll (~1s): a cancel is not urgent to the millisecond,
            # but it must not wait for the whole market either.
            called_off = called_off or cancelled()
            timed_out = called_off or _elapsed(started, deadline_s)
            if not timed_out:
                finished, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
            else:
                # Deadline: give the requests already in flight a few seconds to
                # land, then walk away — blocking on a stalled socket would turn
                # a soft budget into a hard one.
                finished, stragglers = wait(list(in_flight), timeout=5.0)
                for future in stragglers:
                    in_flight.pop(future, None)
                    future.cancel()
            for future in finished:
                symbol = in_flight.pop(future, "")
                try:
                    frame, source = future.result()
                except Exception as exc:  # noqa: BLE001 - one symbol must not abort the run
                    logger.debug("daily fetch failed for %s: %s", symbol, exc)
                    frame, source = pd.DataFrame(), ""
                done += 1
                if frame is None or frame.empty:
                    empty_symbols += 1
                else:
                    written_symbols += 1
                    report.sessions = max(report.sessions, int(frame["date"].nunique()))
                    sink.add(frame, source)
                if not timed_out:
                    submit_next(pool)
            sink.flush()
            now = time.monotonic()
            if done and now - tick >= 1.0:
                tick = now
                rate = done / max(now - started, 1e-6)
                eta = (total - done) / rate if rate > 0 else 0.0
                source_hint = "（已切备用源）" if state.get("tencent_blocked") else ""
                progress(
                    done / max(total, 1),
                    f"日线 {done}/{total} · {rate:.1f} 标的/s · 剩余约 {eta:.0f}s{source_hint}",
                )
                checkpoint({"done": done, "total": total}, done=done, total=total)
            if timed_out:
                break

    sink.flush(force=True)
    report.rows += sink.written
    report.symbols += written_symbols
    # Publish the final position even for a run that finished inside one second:
    # the console's "已完成 x/y" should be true after the job, not only during it.
    checkpoint({"done": done, "total": total}, done=done, total=total)

    if empty_symbols:
        report.skipped += empty_symbols
    remaining = total - done
    if called_off:
        report.notes.append(
            f"已取消，已完成 {done}/{total} 个标的（{written_symbols} 个已落盘）；"
            "再次运行会跳过已覆盖的标的"
        )
    elif remaining > 0:
        report.notes.append(
            f"达到单次时限，已完成 {done}/{total} 个标的，其余 {remaining} 个可再次运行续补"
        )
    if state.get("tencent_blocked"):
        report.notes.append(
            "腾讯源本轮被限流（HTTP 4xx/5xx），已改用新浪日线；新浪最多 1500 根，"
            "五年窗口可能缺最早的一段"
        )
    if not sink.written:
        return ""

    by_source = sink.rows_by_source
    note(
        "写入 " + " / ".join(f"{name} {rows} 行" for name, rows in sorted(by_source.items()))
    )
    if by_source.get("tencent") or by_source.get("sina"):
        report.notes.append("日线源不含成交额，amount 为空；成交额请取 tick_agg 或东财源")
    used = sorted(name for name in by_source if name in {"tencent", "sina"})
    return "+".join(used)


def _normalize_tencent_daily(rows: list[list[Any]], symbol: str) -> pd.DataFrame:
    """Parse ``[date, open, close, high, low, volume]`` rows into the contract."""
    parsed = []
    for row in rows:
        if len(row) < 6:
            continue
        parsed.append(
            {
                "date": row[0],
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": row[5],
            }
        )
    if not parsed:
        return pd.DataFrame()
    frame = pd.DataFrame(parsed)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date", kind="stable")
    if frame.empty:
        return frame
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["symbol"] = symbol
    # 手, matching the intraday dataset and Tushare's ``vol``.  ``amount`` is a
    # float NaN, not ``pd.NA``: an object column would be written to parquet as
    # a string type and break every later numeric read.
    frame["pre_close"] = frame["close"].shift(1)
    frame["pct_chg"] = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
    frame["amount"] = float("nan")
    frame["adj_factor"] = float("nan")
    keep = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pre_close",
        "pct_chg",
        "adj_factor",
    ]
    return frame[[c for c in keep if c in frame.columns]].reset_index(drop=True)


def _ingest_daily_eastmoney(
    universe: Sequence[str],
    start: str,
    end: str,
    report: Any,
    started: float,
    deadline_s: float,
) -> str:
    """Per-symbol raw (``fqt=0``) daily backfill. Returns the source name."""
    from src.data import bars as bar_store
    from src.data.ingest import note, progress

    if not universe:
        report.notes.append("没有可用标的池，请显式传入 symbols")
        return ""

    try:
        from backtest.loaders import eastmoney_client
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"东方财富客户端不可用：{exc}")
        return ""

    beg = start.replace("-", "")
    fin = end.replace("-", "")
    written = 0
    attempted = 0
    for index, symbol in enumerate(universe):
        if _elapsed(started, deadline_s):
            report.notes.append(f"达到单次时限，已完成 {index}/{len(universe)} 个标的")
            break
        if index % 20 == 0:
            progress(index / max(len(universe), 1), f"日线 {symbol}（{index}/{len(universe)}）")
        secid = eastmoney_client.resolve_secid(symbol)
        if not secid:
            report.skipped += 1
            continue
        attempted += 1
        try:
            rows = eastmoney_client.fetch_kline(secid, klt=101, fqt=0, beg=beg, end=fin)
        except Exception as exc:  # noqa: BLE001 - one symbol must not abort the run
            logger.debug("daily fallback failed for %s: %s", symbol, exc)
            report.skipped += 1
            continue
        frame = _normalize_kline(rows, symbol)
        if frame.empty:
            report.skipped += 1
            continue
        rows_written = bar_store.save_long_bars(frame, "1d", source="eastmoney")
        written += rows_written
        report.rows += rows_written
        report.sessions = max(report.sessions, int(frame["date"].nunique()))
        report.symbols += 1

    if written:
        note(f"东方财富写入 {written} 行 / {attempted} 个标的")
        return "eastmoney"
    return ""


def _normalize_kline(rows: list[dict[str, Any]], symbol: str) -> pd.DataFrame:
    """Turn raw kline rows into the lake contract, deriving pre_close/pct_chg."""
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    if "trade_date" not in frame.columns:
        return pd.DataFrame()
    frame = frame.rename(columns={"trade_date": "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date", kind="stable")
    if frame.empty:
        return frame
    frame["symbol"] = symbol
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # Derived inside the fetched window: the first row of a window has no base,
    # so it is left null instead of borrowing a close the caller cannot verify.
    frame["pre_close"] = frame["close"].shift(1)
    frame["pct_chg"] = (frame["close"] / frame["pre_close"] - 1.0) * 100.0
    frame["adj_factor"] = float("nan")
    keep = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "pre_close",
        "pct_chg",
        "adj_factor",
    ]
    return frame[[c for c in keep if c in frame.columns]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Intraday
# ---------------------------------------------------------------------------


def intraday(
    *,
    interval: str = "5m",
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
    deadline_s: float = 600.0,
    cursor: dict[str, Any] | None = None,
) -> Any:
    """Ingest intraday bars, either today's window or a historical range.

    Two modes, chosen by ``start``:

    * **No ``start``** — today's session from Tencent's sliding window.  The feed
      returns the most recent N bars and ignores any date range, so this is
      inherently a *daily* job: run it after the close and the session lands in
      the lake as one immutable partition.  Re-running it for a session that is
      already stored is a no-op upsert, never a duplicate.
    * **``start`` before today** — a range backfill from baostock, which is the
      only free archive that answers minute bars for a *date range* rather than
      a window.  See :func:`_intraday_history`.

    Why the window mode resumes instead of restarting
    -------------------------------------------------
    A full-market session is ~5,500 fetches at ~0.2s, which is 18 minutes — far
    past a UI job's deadline.  The old shape fetched everything into memory and
    wrote once at the end, so the deadline stopped the run at the same symbol
    every time and the tail was never reached: a livelock, not a slow job.

    Two things fix it.  Bars are written every
    :data:`_INTRADAY_CHUNK_SYMBOLS` symbols, so a kill keeps what was already
    paid for; and a re-run asks the lake which symbols already have this session,
    so it starts at the tail.  The *lake* is the cursor here, because it is the
    only thing that knows a symbol's write actually landed — ``cursor`` is
    accepted and reported for the console ("已完成 x/y"), never trusted for
    coverage.
    """
    from src.data import bars as bar_store
    from src.data.ingest import IngestReport, cancelled, checkpoint, note, progress

    started = time.monotonic()
    report = IngestReport(dataset="bars_intraday")

    from src.data import hub

    # ``AsOf.date`` is an ISO string, not a ``date`` — this used to call
    # ``.isoformat()`` on it, which meant the intraday path crashed on its first
    # real request and no test noticed because none exercised it end to end.
    as_of = hub.resolve_as_of().date
    session = str(as_of)

    if start and str(start)[:10] < session:
        return _intraday_history(
            interval=interval,
            symbols=symbols,
            start=str(start)[:10],
            end=str(end)[:10] if end else session,
            session=session,
            limit=limit,
            refresh=refresh,
            deadline_s=deadline_s,
            cursor=cursor,
            report=report,
            started=started,
        )

    window_start = (dt.date.fromisoformat(session) - dt.timedelta(days=7)).isoformat()

    universe = [_canonical(s) for s in (symbols or default_universe(limit))]
    universe = [s for s in dict.fromkeys(universe) if s]
    if limit:
        universe = universe[: int(limit)]
    if not universe:
        report.notes.append("没有可用标的池，请显式传入 symbols")
        return report

    # Coverage is judged per symbol, never from the dataset watermark.  The
    # watermark is "the newest session *any* symbol has", so using it here meant
    # that the first partial run made every later run report "已入库" and the
    # tail of the market was never fetched.
    covered = set() if refresh else _intraday_covered(interval, session)
    pending = [s for s in universe if s not in covered]
    report.skipped = len(universe) - len(pending)
    report.start = report.end = session
    if not pending:
        report.notes.append(
            f"{len(universe)} 个标的的 {session} {interval} 均已在湖内，无需重取"
        )
        progress(1.0, "分钟线已覆盖")
        return report

    from backtest.loaders.registry import get_loader_cls_with_fallback

    try:
        loader = get_loader_cls_with_fallback("tencent")()
    except Exception as exc:  # noqa: BLE001 - registry/import drift
        report.notes.append(f"腾讯分钟线加载器不可用：{exc}")
        return report

    total = len(pending)
    resumed = ""
    if cursor and cursor.get("session") == session and int(cursor.get("done") or 0) > 0:
        resumed = f"（续跑：上次完成 {cursor.get('done')} 个）"
    note(
        f"{interval} 拉取 {total} 个标的（{session}{resumed}；"
        f"已在湖内 {report.skipped} 个）"
    )

    buffer: dict[str, pd.DataFrame] = {}
    written_rows = 0
    written_symbols = 0
    sessions_seen: set[str] = set()
    stopped = False

    def flush() -> None:
        nonlocal written_rows, written_symbols
        if not buffer:
            return
        for frame in buffer.values():
            sessions_seen.update(
                pd.to_datetime(frame.index).strftime("%Y-%m-%d").unique().tolist()
            )
        written_rows += bar_store.save_bars(buffer, interval, source="tencent")
        written_symbols += len(buffer)
        buffer.clear()

    for index, symbol in enumerate(pending):
        if cancelled():
            report.notes.append(f"收到取消请求，停在 {index}/{total}")
            stopped = True
            break
        if _elapsed(started, deadline_s):
            report.notes.append(f"达到单次时限，已完成 {index}/{total} 个标的")
            stopped = True
            break
        if index % 20 == 0:
            progress(index / total, f"{interval} {symbol} {index}/{total}")
        try:
            fetched = loader.fetch(
                [symbol],
                window_start,
                session,
                interval=interval,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("intraday fetch failed for %s: %s", symbol, exc)
            report.skipped += 1
            continue
        frame = fetched.get(symbol)
        if frame is None or frame.empty:
            report.skipped += 1
            continue
        buffer[symbol] = frame
        if len(buffer) >= _INTRADAY_CHUNK_SYMBOLS:
            flush()
            # Checkpoint after the write, never before: the cursor must describe
            # work that is already durable.
            checkpoint(
                {"session": session, "after": symbol, "done": index + 1, "total": total},
                done=index + 1,
                total=total,
            )

    flush()
    if written_symbols:
        checkpoint(
            {"session": session, "done": written_symbols, "total": total},
            done=written_symbols,
            total=total,
        )

    report.rows = written_rows
    report.symbols = written_symbols
    report.sessions = len(sessions_seen) or 1
    if stopped and written_symbols < total:
        report.notes.append(
            f"本轮写入 {written_symbols}/{total} 个标的；再次运行只补剩下的（湖内已覆盖的会跳过）"
        )
    report.notes.append("分钟线源为滚动窗口，历史只能逐日累积")
    progress(1.0, "分钟线入库完成")
    return report


# Symbols buffered before one intraday write.  400 keeps memory small and makes
# a killed run lose at most a couple of minutes of fetching, while still
# rewriting the session partition ~14 times instead of once per symbol.
_INTRADAY_CHUNK_SYMBOLS = 400


def _intraday_covered(interval: str, session: str) -> set[str]:
    """Symbols that already have at least one bar for *session*.

    One aggregate over the session partition: the alternative — loading bars
    per symbol to "check first" — would cost more than the fetch it avoids.
    """
    from src.data import lake
    from src.data.bars import dataset_for

    frame = lake.aggregate_dataset(
        dataset_for(interval),
        "symbol, count(*) AS bars",
        group_by="1",
        where=(
            'CAST("date" AS DATE) = CAST(? AS DATE) AND "interval" = ?',
            [session, interval],
        ),
    )
    if frame.empty:
        return set()
    return set(frame["symbol"].astype(str))


# ---------------------------------------------------------------------------
# Historical range backfill
# ---------------------------------------------------------------------------
#
# Why this needs its own path
# ---------------------------
# The window fetch above can only ever fill *today*.  A five-year minute history
# cannot be assembled from a 320-bar window: Eastmoney's 5-minute feed returns
# 1,536 bars (32 sessions) no matter what ``lmt`` asks for, Tencent's ``mkline``
# returns 320, and Tushare's ``stk_mins`` is rate-limited to one call per minute
# on a standard plan — one symbol every sixty seconds is not a backfill.
#
# baostock is the one free source that answers a *date range* for minute bars,
# back to 2019-01-02 (measured: 2021 data present, 2019-01 returns empty).  One
# call per symbol per year, ~700 rows/second on this machine, unadjusted prices
# (which is exactly what the lake stores) plus ``amount``.

# Minute bars start here; an earlier request answers "no rows", which would
# otherwise be indistinguishable from "this symbol was suspended".
_INTRADAY_HISTORY_FROM = "2019-01-02"
# baostock offers 5/15/30/60-minute bars and nothing finer.  Keys are the
# *normalised* tokens ``normalize_interval`` produces (``60m`` becomes ``1h``),
# so an hourly backfill is not silently refused.
_BS_MINUTE_FREQ = {"5m": "5", "15m": "15", "30m": "30", "1h": "60"}
# Rows buffered before a write.  The write path is read-modify-write per session
# partition, so the batch size *is* the write amplification: at 4M rows a
# five-year, 5,000-symbol backfill rewrites each of its ~1,200 session files
# about seventy times instead of once per symbol (~12,000 rewrites per session).
_INTRADAY_BUFFER_ROWS = 4_000_000
# Which (symbol, year) pairs have already been asked for.  Without this, a
# symbol listed in 2024 is re-fetched for 2019-2023 on every run *forever*,
# because "no bars" and "not fetched" look identical in the lake.
_INTRADAY_ATTEMPTS = "intraday_attempts.json"


def _bs_minute_freq(interval: str) -> str | None:
    """Map ``5m`` to baostock's ``5``; ``None`` when it has no such bar size."""
    from backtest.loaders._intervals import normalize_interval

    # ``normalize_interval`` spells the hourly bar ``1H``; comparing lowercased
    # keeps the table readable and the match case-insensitive.
    return _BS_MINUTE_FREQ.get(normalize_interval(interval).lower())


def _attempts_path():
    from src.data import lake

    return lake.lake_root() / "_ingest" / _INTRADAY_ATTEMPTS


def _read_attempts() -> dict[str, str]:
    """``"interval|SYMBOL|year" -> date`` of every range request already made."""
    try:
        raw = json.loads(_attempts_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing or corrupt cache is "no cache"
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, stamp in raw.items():
        parts = str(key).split("|")
        if len(parts) == 2:
            # Rows written before the interval prefix existed: the range path
            # shipped with one bar size, so they can only be 5m.  Without this
            # migration a 5m run would keep hourly backfills permanently skipped.
            key = f"5m|{parts[0]}|{parts[1]}"
        out[str(key)] = str(stamp)
    return out


def _save_attempts(attempts: dict[str, str]) -> None:
    path = _attempts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(attempts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - the cache is an optimisation
        logger.debug("intraday attempts write failed: %s", exc)


def _intraday_year_counts(
    interval: str, start: str, end: str
) -> dict[tuple[str, int], int]:
    """Sessions already stored per ``(symbol, year)`` inside the range.

    One scan, not one query per symbol: the alternative would be 5,000 round
    trips to answer a question DuckDB settles in seconds.
    """
    from src.data import lake
    from src.data.bars import dataset_for

    frame = lake.aggregate_dataset(
        dataset_for(interval),
        'symbol, year("date") AS y, count(DISTINCT CAST("date" AS DATE)) AS n',
        group_by="1, 2",
        where=(
            'CAST("date" AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) '
            'AND "interval" = ?',
            [start, end, interval],
        ),
    )
    if frame.empty:
        return {}
    return {
        (str(row.symbol), int(row.y)): int(row.n)
        for row in frame.itertuples(index=False)
    }


def _range_complete(
    symbol: str,
    year_need: dict[int, int],
    counts: dict[tuple[str, int], int],
    attempts: dict[str, str],
    interval: str,
) -> bool:
    """True when every year of the range is either stored or already asked for.

    A year counts as done when the lake holds as many sessions as the calendar
    has, *or* when that symbol-year was requested before: a 2024 listing has no
    2019 bars, and re-asking would never change that.  The interval is part of
    the attempt key — 5m coverage says nothing about whether hourly bars were
    ever asked for.
    """
    for year, need in year_need.items():
        if counts.get((symbol, year), 0) >= need:
            continue
        if f"{interval}|{symbol}|{year}" in attempts:
            continue
        return False
    return True


def _intraday_history(
    *,
    interval: str,
    symbols: Sequence[str] | None,
    start: str,
    end: str,
    session: str,
    limit: int | None,
    refresh: bool,
    deadline_s: float,
    cursor: dict[str, Any] | None,
    report: Any,
    started: float,
) -> Any:
    """Backfill ``[start, end]`` of minute bars from baostock.

    Work unit is one symbol over the whole range: baostock prices a request by
    the rows it returns, not by the calls it takes, so five years in one call is
    ~58k rows for the price of one round trip.  Rows are buffered and written in
    batches of :data:`_INTRADAY_BUFFER_ROWS`, because the per-session partition
    write is read-modify-write — flushing per symbol would rewrite a session file
    once per symbol instead of once per batch.
    """
    from src.data import bars as bar_store
    from src.data import calendar
    from src.data.ingest import cancelled, checkpoint, note, progress

    from backtest.loaders._intervals import normalize_interval

    # The lake keys on the normalised token (``5min`` and ``5m`` are the same
    # bar), so every query against stored rows must use it, not the caller's
    # spelling — otherwise a perfectly covered range reads back as empty.
    token = normalize_interval(interval)
    freq = _bs_minute_freq(token)
    if freq is None:
        report.notes.append(
            f"baostock 分钟线只提供 5/15/30/60 分钟，{interval} 没有可回补的历史源"
        )
        return report

    end = min(end, session)
    if start < _INTRADAY_HISTORY_FROM:
        report.notes.append(
            f"baostock 分钟线自 {_INTRADAY_HISTORY_FROM} 起，更早的区间无法回补"
        )
    first = max(start, _INTRADAY_HISTORY_FROM)
    if first > end:
        report.notes.append(f"区间 {start}~{end} 在分钟线历史范围之外")
        return report

    sessions = calendar.trading_days(first, end)
    if not sessions:
        report.notes.append(f"{first}~{end} 没有交易日")
        return report
    year_need: dict[int, int] = {}
    for day in sessions:
        year_need[int(day[:4])] = year_need.get(int(day[:4]), 0) + 1
    report.start, report.end = first, end

    universe = [_canonical(s) for s in (symbols or default_universe(None))]
    universe = [s for s in dict.fromkeys(universe) if s]
    if limit:
        universe = universe[: int(limit)]
    if not universe:
        report.notes.append("没有可用标的池，请显式传入 symbols")
        return report

    counts = {} if refresh else _intraday_year_counts(token, first, end)
    attempts = {} if refresh else _read_attempts()

    pending: list[str] = []
    unsupported = 0
    for symbol in universe:
        if _bs_code(symbol) is None:
            # Beijing is rejected outright by baostock — counted, never faked.
            unsupported += 1
            continue
        if not refresh and _range_complete(symbol, year_need, counts, attempts, token):
            report.skipped += 1
            continue
        pending.append(symbol)

    total = len(pending)
    if not total:
        report.notes.append(
            f"{len(universe)} 个标的的 {first}~{end} {interval} 均已在湖内，无需重取"
        )
        progress(1.0, "分钟线已覆盖")
        return report

    resumed = ""
    if isinstance(cursor, dict) and cursor.get("mode") == "range" and cursor.get("after"):
        resumed = f"；上次停在 {cursor['after']}"
    note(
        f"{interval} 回补 {total} 个标的 × {len(sessions)} 个交易日"
        f"（{first}~{end}{resumed}；已在湖内 {report.skipped} 个）"
    )

    client = _BaostockMinutes()
    buffer: list[tuple[str, pd.DataFrame]] = []
    buffered_rows = 0
    written_rows = 0
    written_symbols = 0
    processed = 0
    sessions_seen: set[str] = set()
    stopped = False

    def flush() -> None:
        nonlocal buffered_rows, written_rows, written_symbols
        if not buffer:
            return
        written_rows += bar_store.save_bars(dict(buffer), token, source="baostock")
        written_symbols += len(buffer)
        buffer.clear()
        buffered_rows = 0

    attempted: set[str] = set()
    try:
        for index, symbol in enumerate(pending):
            if cancelled():
                report.notes.append(f"收到取消请求，停在 {index}/{total}")
                stopped = True
                break
            if _elapsed(started, deadline_s):
                report.notes.append(f"达到单次时限，已完成 {index}/{total} 个标的")
                stopped = True
                break
            if index % 10 == 0:
                progress(index / total, f"{interval} {symbol} {index}/{total}")
            try:
                frame = client.bars(symbol, first, end, freq)
            except Exception as exc:  # noqa: BLE001 - socket/format drift
                client.failed += 1
                if client.failed <= 3:
                    report.notes.append(f"{symbol} 分钟线取数失败：{exc}")
                client._reconnect()
                continue
            # Marked *after* a reply arrives: a symbol that failed mid-range must
            # be retried, but one that answered "no bars" must not be.
            attempted.update(f"{token}|{symbol}|{year}" for year in year_need)
            processed += 1
            if frame.empty:
                continue
            sessions_seen.update(frame.index.strftime("%Y-%m-%d").unique().tolist())
            buffer.append((symbol, frame))
            buffered_rows += len(frame)
            if buffered_rows >= _INTRADAY_BUFFER_ROWS:
                flush()
                # Only after the write: a cursor must describe durable work.
                checkpoint(
                    {
                        "mode": "range",
                        "start": first,
                        "end": end,
                        "after": symbol,
                        "done": processed,
                        "total": total,
                    },
                    done=processed,
                    total=total,
                )
        flush()
    finally:
        client.close()
        if attempted:
            attempts.update(
                {key: dt.date.today().isoformat() for key in attempted}
            )
            _save_attempts(attempts)

    report.rows = written_rows
    report.symbols = written_symbols
    report.sessions = len(sessions_seen)
    report.source = "baostock"
    elapsed = max(time.monotonic() - started, 0.01)
    report.notes.append(
        f"来源 baostock（{_INTRADAY_HISTORY_FROM} 起，不复权）；"
        "volume 已由股换算为手，amount 单位为元"
    )
    if processed >= 5 and written_rows:
        # A single-symbol run is dominated by baostock's ~25s cold start, and
        # quoting "1 row/second" off it would misread as a broken backfill.
        report.notes.append(f"本次吞吐 {written_rows / elapsed:.0f} 行/秒")
    if client.empty:
        report.notes.append(f"{client.empty} 个标的在区间内没有分钟线（新股或长期停牌）")
    if client.failed:
        report.notes.append(f"{client.failed} 个标的取数失败，下次运行会重试")
    if unsupported:
        report.notes.append(f"{unsupported} 个标的 baostock 不支持（如北交所），已跳过")
    if stopped and processed < total:
        report.notes.append(
            f"本轮完成 {processed}/{total} 个标的；再次运行只补剩下的（已入库的会跳过）"
        )
    progress(1.0, "分钟线回补完成")
    return report
