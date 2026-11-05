"""Trading-calendar ingestion — the reference every other job is scheduled by.

The calendar is not a market fact to be discovered per screen: ``as_of``,
backtest annualisation and "how many sessions behind" all read it.  This module
is a thin job wrapper so the console can refresh it the same way it refreshes
bars, and so a refresh reports *when* the new seed ends instead of silently
swapping the file under a running process.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["refresh"]


def refresh(*, deadline_s: float = 600.0) -> Any:
    """Re-seed the calendar from AKShare and return what changed.

    ``deadline_s`` is accepted for the runner's uniform signature and ignored:
    this is a single HTTP call, and abandoning it halfway would leave the cache
    in exactly the state the caller was trying to fix.
    """
    from src.data import calendar as calendar_store
    from src.data.ingest import IngestReport, note, progress

    del deadline_s
    report = IngestReport(dataset="calendar", source="akshare")
    progress(0.1, "拉取交易日历")
    count = calendar_store.refresh_calendar()
    dates = calendar_store.calendar_dates()

    report.rows = count
    report.sessions = count
    report.symbols = 1
    report.start = dates[0].isoformat() if dates else None
    report.end = dates[-1].isoformat() if dates else None
    note(f"交易日历 {count} 个交易日，末日 {report.end}")
    progress(1.0, "交易日历已刷新")
    return report
