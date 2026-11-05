"""Daily A-share paper book driven by monitor tradable signals.

The book simulates one session at a time with the same cost and fill rules as
``ChinaAEngine`` (``backtest.ashare_costs``). It is not the shadow-account
journal extractor.
"""

from src.paper_book.book import PaperBookError, step_day
from src.paper_book.models import DailyReport

__all__ = ["DailyReport", "PaperBookError", "step_day"]
