"""Tradability-panel ingestion — derived from stored daily bars, not refetched.

The panel answers "could this order have filled?" (halt, ST, price band).  Its
inputs are the daily bars already in the lake plus a universe snapshot, so this
job deliberately performs **no network call**: rebuilding the panel from the
same bars the backtest reads is what keeps "stop-loss at -10%" and "the band was
±20%" from disagreeing.

That also makes the dependency explicit.  An empty ``bars_daily`` is reported as
"fill daily bars first", because deriving a panel from nothing would produce a
plausible-looking table of halts that never happened.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["daily"]

# Building the panel means one reindex per symbol across the window; beyond a
# few years x a few thousand names it becomes a job of its own, so a single run
# stays inside a year unless the caller asks for more.
DEFAULT_PANEL_SESSIONS = 250


def daily(
    *,
    start: str | None = None,
    end: str | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    refresh: bool = False,
    deadline_s: float = 600.0,
) -> Any:
    """Rebuild the tradability panel for a window from ``bars_daily``.

    Args:
        start: Inclusive first session; defaults to the lake's bar watermark
            minus :data:`DEFAULT_PANEL_SESSIONS`.
        end: Inclusive last session; defaults to the resolved ``as_of``.
        symbols: Optional whitelist.
        limit: Cap on symbols.
        refresh: Accepted for signature parity; a panel rebuild always recomputes
            the requested window (upsert makes that idempotent).
        deadline_s: Soft wall-clock budget.

    Returns:
        :class:`~src.data.ingest.IngestReport`.
    """
    from src.data import bars as bar_store
    from src.data import calendar
    from src.data import tradability
    from src.data.ingest import IngestReport, note, progress

    del refresh
    started = time.monotonic()
    report = IngestReport(dataset="tradability")

    from src.data import hub

    as_of = hub.resolve_as_of().date
    end_day = pd.Timestamp(end).date() if end else as_of

    watermark = bar_store.watermark("1d")
    if not watermark:
        report.notes.append("日线尚未入库，请先补 bars_daily")
        return report

    if start:
        start_day = pd.Timestamp(start).date()
    else:
        sessions = calendar.trading_days(
            (end_day - dt.timedelta(days=DEFAULT_PANEL_SESSIONS * 2)).isoformat(),
            end_day.isoformat(),
        )
        start_day = pd.Timestamp(sessions[-DEFAULT_PANEL_SESSIONS]).date()

    report.start, report.end = start_day.isoformat(), end_day.isoformat()
    sessions = calendar.trading_days(report.start, report.end)
    if not sessions:
        report.notes.append("区间内没有交易日")
        return report

    progress(0.1, "读取日线")
    frame = bar_store.load_bars(
        "1d",
        symbols=list(symbols) if symbols else None,
        start=report.start,
        end=report.end,
    )
    if frame.empty:
        report.notes.append("该区间没有日线数据")
        return report

    per_symbol = bar_store.bars_by_symbol(frame)
    if limit:
        per_symbol = dict(list(per_symbol.items())[: int(limit)])
    if time.monotonic() - started >= deadline_s > 0:
        report.notes.append("达到单次时限，已处理 0 个标的")
        return report

    progress(0.4, f"构建面板：{len(per_symbol)} 个标的 / {len(sessions)} 个交易日")
    panel = tradability.build_panel(
        per_symbol,
        sessions=[dt.date.fromisoformat(s) for s in sessions],
    )
    if panel.empty:
        report.notes.append("面板为空（日线缺少 close/volume）")
        return report

    progress(0.85, "写入面板")
    report.rows = tradability.save_panel(panel)
    report.symbols = int(panel["symbol"].nunique())
    report.sessions = int(panel["date"].nunique())
    report.source = "bars_daily"

    mismatched = int(panel["band_mismatch"].fillna(False).sum()) if "band_mismatch" in panel else 0
    suspended = int(panel["is_suspended"].fillna(False).sum()) if "is_suspended" in panel else 0
    report.notes.append(f"停牌 {suspended} 行、价格带矛盾 {mismatched} 行")
    report.notes.append("ST 标记来自标的池快照；缺失时按非 ST 处理")
    note(f"面板 {report.rows} 行 / {report.symbols} 个标的")
    progress(1.0, "面板已刷新")
    return report
