"""Signal monitor board — persistent universe + strategy scan for action suggestions.

Research-only: produces buy/sell/hold suggestions and trackable daily holdings
from a bound SignalEngine. Does not place orders or replace a brokerage quote terminal.
"""

from __future__ import annotations

from src.monitor.models import (
    DayOps,
    HoldingRow,
    MonitorConfig,
    MonitorDaySnapshot,
    MonitorSuggestion,
    StrategyRef,
)
from src.monitor.scan import run_monitor_scan
from src.monitor.store import (
    board_id_for_strategy,
    load_board_for_strategy,
    load_monitor,
    save_monitor,
)

__all__ = [
    "DayOps",
    "HoldingRow",
    "MonitorConfig",
    "MonitorDaySnapshot",
    "MonitorSuggestion",
    "StrategyRef",
    "board_id_for_strategy",
    "load_board_for_strategy",
    "load_monitor",
    "save_monitor",
    "run_monitor_scan",
]
