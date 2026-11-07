"""Tick-aggregate ingestion — one row per (symbol, session) from the print feed.

This is the expensive per-symbol feed (a liquid name is ~55 pages of prints), so
the job is deliberately small: a watchlist-sized universe, after the close, once
per session.  Asking for "the whole market" here is not a slow job, it is a job
that cannot finish — hence the low default cap and the explicit note when it is
hit.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = ["daily", "DEFAULT_SYMBOLS"]

# ~55 pages per liquid name; 30 names is already ~1650 requests.
DEFAULT_SYMBOLS = 30


def daily(
    *,
    symbols: Sequence[str] | None = None,
    limit: int = DEFAULT_SYMBOLS,
    refresh: bool = False,
    deadline_s: float = 600.0,
) -> Any:
    """Aggregate the most recent session's prints for a symbol list.

    Args:
        symbols: Watchlist; defaults to the symbols already in the lake.
        limit: Cap on names per run (the feed cannot serve the whole market).
        refresh: Re-aggregate a session already stored (idempotent upsert).
        deadline_s: Soft wall-clock budget.

    Returns:
        :class:`~src.data.ingest.IngestReport`.
    """
    from src.data import ticks as tick_store
    from src.data.ingest import IngestReport, note, progress

    del refresh
    started = time.monotonic()
    report = IngestReport(dataset="tick_agg", source="tencent")

    session = tick_store.today_session()
    if not tick_store.session_is_complete():
        report.notes.append(f"{session} 尚未收盘，逐笔信号不完整；收盘后再跑")
        return report

    universe = list(symbols) if symbols else _default_symbols(limit)
    universe = [str(s).strip() for s in universe if str(s).strip()][: max(int(limit), 1)]
    if not universe:
        report.notes.append("没有可用标的池，请显式传入 symbols")
        return report

    note(f"{session} 逐笔聚合 {len(universe)} 个标的")
    rows: list[dict[str, Any]] = []
    for index, symbol in enumerate(universe):
        if deadline_s > 0 and (time.monotonic() - started) >= deadline_s:
            report.notes.append(f"达到单次时限，已完成 {index}/{len(universe)} 个标的")
            break
        progress(index / max(len(universe), 1), f"逐笔 {symbol}")
        try:
            prints = tick_store.fetch_ticks(symbol, trade_date=session)
        except Exception as exc:  # noqa: BLE001 - one name must not abort the run
            logger.debug("tick fetch failed for %s: %s", symbol, exc)
            report.skipped += 1
            continue
        if prints is None or prints.empty:
            report.skipped += 1
            continue
        aggregated = tick_store.aggregate(prints, symbol, trade_date=session)
        if aggregated:
            rows.append(aggregated)

    if not rows:
        report.notes.append("未取到逐笔数据（停牌或源站无该日成交）")
        return report

    report.rows = tick_store.save(rows)
    report.symbols = len(rows)
    report.sessions = 1
    report.start = report.end = session
    note(f"逐笔聚合 {report.rows} 行")
    progress(1.0, "逐笔聚合完成")
    return report


def _default_symbols(limit: int) -> list[str]:
    """Prefer names already under coverage: same universe, no surprise spend."""
    from src.data.ingest.bars import default_universe

    return default_universe(limit)
