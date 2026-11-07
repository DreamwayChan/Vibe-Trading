"""Wind -> factor screen -> factor card pipeline (research-only).

For one sector: detect wind, and only when the sector is in an actionable
state with a validated wind score, screen library factors and emit cards that
record when the factor applies and when it must be retired.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.sector_pick.factor_select import FactorVerdict, daily_rank_ic, forward_returns, screen_factors
from src.sector_pick.wind_scan import evaluate_sector_wind

DECAY_WINDOW = 20
DECAY_FLOOR = 0.0


def build_factor_card(
    verdict: FactorVerdict,
    wind: dict,
    sector: str,
    *,
    horizon: int,
) -> dict:
    return {
        "sector": sector,
        "factor": verdict.name,
        "entry": "T 日收盤出信號，T+1 開盤買入（只做多，開盤漲停不買）",
        "horizon_days": horizon,
        "shrunk_ic": round(verdict.shrunk_ic, 4),
        "ic_in": round(verdict.ic_in, 4),
        "ic_out": round(verdict.ic_out, 4),
        "t_out": round(verdict.t_out, 2),
        "applies_when": {"wind_states": ["launch", "main"], "wind_state_now": wind.get("state")},
        "retire_when": {
            "rolling_ic_window": DECAY_WINDOW,
            "rolling_ic_below": DECAY_FLOOR,
            "wind_states": ["fade", "dormant"],
        },
    }


def decay_check(
    factor: pd.DataFrame,
    open_: pd.DataFrame,
    close: pd.DataFrame,
    *,
    horizon: int = 5,
    window: int = DECAY_WINDOW,
) -> dict:
    """Rolling-IC retirement test using only matured forward returns."""
    fwd = forward_returns(open_, close, horizon)
    ic = daily_rank_ic(factor.reindex_like(close), fwd)
    if len(ic) < window:
        return {"retire": False, "rolling_ic": None}
    rolling = float(ic.iloc[-window:].mean())
    return {"retire": rolling < DECAY_FLOOR, "rolling_ic": round(rolling, 4)}


def build_sector_cards(
    sector: str,
    data_map: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    factors: dict[str, pd.DataFrame],
    *,
    horizon: int = 5,
    prior_ic: dict[str, float] | None = None,
) -> dict:
    wind = evaluate_sector_wind(data_map, benchmark, horizon)
    if not wind.get("actionable"):
        return {"sector": sector, "wind": wind, "cards": [], "reason": "wind not actionable"}

    close = pd.DataFrame({c: f["close"] for c, f in data_map.items()}).sort_index()
    opens = pd.DataFrame({c: f["open"] for c, f in data_map.items()}).reindex_like(close)
    verdicts = screen_factors(factors, opens, close, horizon=horizon, prior_ic=prior_ic)
    cards = []
    for v in verdicts:
        if not v.selected:
            continue
        card = build_factor_card(v, wind, sector, horizon=horizon)
        card["decay"] = decay_check(factors[v.name], opens, close, horizon=horizon)
        if card["decay"]["retire"]:
            continue
        cards.append(card)
    return {"sector": sector, "wind": wind, "cards": cards,
            "screened": [v.to_dict() for v in verdicts]}
