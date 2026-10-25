
# ============================================================
# 中文名称: 压缩低位吸筹 (COMP_LOW)
# 简要说明: 中期区间低位 × 短期波动压缩；同花顺「主力吸货」的干净经济重写。
# 典型用途: 作为动量主排序的确认因子（低位蓄势），不宜单独当作主力资金真值。
# ============================================================
"""custom_comp_low — low-zone strength after range compression.

Economic meaning (rewritten from THS-style "主力真吸货", not a formula dump):
    Prefer names that sit near the lower end of a medium-term price range
    *and* have coiled (short-range width small vs longer-range width).
    That combination is a clean proxy for "absorption / accumulation at lows
    after volatility compression".

Formula:
    near_low_38 = 1 - (close - LLV(low,38)) / (HHV(high,38) - LLV(low,38))
    compression = max(0, 1 - range(5) / range(40))
    factor = near_low_38 * compression

Research (BK1184 robot theme, 2024-01..2026-07, fwd-5d rank IC):
    IC≈+0.051, IR≈0.29, 5-group mono≈+0.9, mean |corr| vs gtja191_164≈0.13.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_max, ts_min

__alpha_meta__ = {
    "id": "custom_comp_low",
    "nickname": "compressed low-zone absorb",
    "theme": ["reversal", "volatility"],
    "formula_latex": (
        r"(1-\mathrm{RSV}_{38})\,"
        r"\max\!\bigl(0,\,1-\mathrm{range}_5/\mathrm{range}_{40}\bigr)"
    ),
    "columns_required": ["close", "high", "low"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 40,
    "min_warmup_bars": 40,
    "notes": (
        "Clean rewrite of THS '主力吸货': low relative position × range "
        "compression. Chart-only ops (STICKLINE) dropped. Tuned windows "
        "L38 + C5/40 on BK1184 panel; complementary to gtja191_164."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return compressed low-zone absorption score (higher = stronger).

    Args:
        panel: Wide OHLCV frames; needs close/high/low.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    high = panel["high"]
    low = panel["low"]

    # Medium-term location: 1 at the 38d low, 0 at the 38d high.
    range_38 = ts_max(high, 38) - ts_min(low, 38)
    near_low = 1.0 - safe_div(close - ts_min(low, 38), range_38)

    # Coil proxy: short range small relative to a slightly longer range.
    range_5 = ts_max(high, 5) - ts_min(low, 5)
    range_40 = ts_max(high, 40) - ts_min(low, 40)
    compression = (1.0 - safe_div(range_5, range_40)).clip(lower=0.0)

    return near_low * compression
