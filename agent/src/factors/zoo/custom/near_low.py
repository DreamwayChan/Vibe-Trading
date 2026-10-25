
# ============================================================
# 中文名称: 低位相对位置 (NEAR_LOW)
# 简要说明: 1 - RSV38，衡量收盘价贴近中期低点的程度。
# 典型用途: 吸筹/低位确认的单因子组件；可与 custom_comp_low 或动量因子组合。
# ============================================================
"""custom_near_low — closeness of close to the 38-day low of the range.

Economic meaning:
    A pure "how close is price to the medium-term trough" score. This is the
    location leg of the THS absorb idea, without the broken IF/STICKLINE
    scaffolding. Higher = closer to the 38d low.

Formula:
    near_low_38 = 1 - (close - LLV(low,38)) / (HHV(high,38) - LLV(low,38))

Research (BK1184 robot theme, 2024-01..2026-07, fwd-5d rank IC):
    IC≈+0.042, IR≈0.24, mean corr vs gtja191_164≈0.11.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_max, ts_min

__alpha_meta__ = {
    "id": "custom_near_low",
    "nickname": "38d near-low position",
    "theme": ["reversal"],
    "formula_latex": (
        r"1 - "
        r"\frac{\mathrm{close}-\mathrm{LLV}(\mathrm{low},38)}"
        r"{\mathrm{HHV}(\mathrm{high},38)-\mathrm{LLV}(\mathrm{low},38)}"
    ),
    "columns_required": ["close", "high", "low"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 38,
    "min_warmup_bars": 38,
    "notes": (
        "Location component of THS-style absorb: 1-RSV38. Keep as a separate "
        "confirm factor when compression gating is undesired."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return 38d near-low score (higher = closer to range low).

    Args:
        panel: Wide OHLCV frames; needs close/high/low.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]
    range_38 = ts_max(high, 38) - ts_min(low, 38)
    return 1.0 - safe_div(close - ts_min(low, 38), range_38)
