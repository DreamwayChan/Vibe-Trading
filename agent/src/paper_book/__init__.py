"""Daily A-share paper book driven by monitor tradable signals.

The book simulates one session at a time with the same cost and fill rules as
``ChinaAEngine`` (``backtest.ashare_costs``). ``step_range`` walks consecutive
exchange sessions and writes the window report. It is not the shadow-account
journal extractor.
"""

from src.paper_book.book import PaperBookError, step_day
from src.paper_book.models import DailyReport, WindowReport
from src.paper_book.soak import step_range

__all__ = ["DailyReport", "PaperBookError", "WindowReport", "step_day", "step_range"]
