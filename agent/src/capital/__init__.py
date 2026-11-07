"""A-share capital-flow monitoring (ETF-OI / national-team flow proxy).

The module tracks how large a share of the CSI 300 free-float market the
headline CSI 300 index funds represent, and derives a daily subscription /
redemption tape from exchange-published fund shares.

Coordinates with the rest of the app:
    * ``src.capital.sources``  — external data adapters + on-disk cache
    * ``src.capital.etf_oi``   — the deterministic model (no LLM, no look-ahead)
    * ``src.capital.store``    — series persistence under ``~/.vibe-trading/``
    * ``src.api.capital_routes`` — HTTP surface consumed by the Web UI
"""

from __future__ import annotations

from src.capital.models import (
    CapitalPoint,
    EtfFundDef,
    EtfOiReport,
    FlowStats,
    FundDailyRow,
    McapReference,
)

__all__ = [
    "CapitalPoint",
    "EtfFundDef",
    "EtfOiReport",
    "FlowStats",
    "FundDailyRow",
    "McapReference",
]
