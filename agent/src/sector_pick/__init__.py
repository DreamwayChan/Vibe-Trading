"""Cross-sector pick: rank researched optimal boards and load winner holdings.

Research-only. Does not place orders. Candidate set is the curated 「最优」
catalog, not an automatic sweep of every backtest run.
"""

from __future__ import annotations

from src.sector_pick.catalog import CATALOG, SectorRecipe, recipe_by_key
from src.sector_pick.models import SectorPickResult, SectorScore
from src.sector_pick.scan import SectorPickError, run_sector_pick_scan
from src.sector_pick.store import load_state, save_state

__all__ = [
    "CATALOG",
    "SectorPickError",
    "SectorPickResult",
    "SectorRecipe",
    "SectorScore",
    "load_state",
    "recipe_by_key",
    "run_sector_pick_scan",
    "save_state",
]
