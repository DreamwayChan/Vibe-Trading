# ============================================================
# 中文名称: 逆彩票MAX (MAX_INV)
# 简要说明: 近月最大单日涨幅越小越好（反彩票偏好）。Bali et al.(2011)。
# 典型用途: 动量确认——剔除刚经历极端暴涨、易回落的标的。
# ============================================================
"""custom_max_inv — inverted MAX lottery demand (Bali et al. 2011).

Economic meaning:
    Investors overpay for lottery-like stocks that just posted an extreme
    daily gain (MAX). Those names subsequently underperform. Sign-flip MAX
    so higher scores prefer stocks *without* a recent extreme up-day —
    a clean risk/behavioral filter alongside momentum.

Formula:
    r_t = close_t / close_{t-1} - 1
    MAX = ts_max(r, 20)
    factor = -MAX                  (higher = no recent lottery spike)

References:
    Bali, T. G., Cakici, N., & Whitelaw, R. F. (2011).
    "Maxing Out: Stocks as Lotteries and the Cross-Section of Expected
    Returns." Journal of Financial Economics.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_max

__alpha_meta__ = {
    "id": "custom_max_inv",
    "nickname": "inverted MAX lottery return (20d)",
    "theme": ["sentiment", "volatility"],
    "formula_latex": r"-\,\mathrm{ts\_max}(r_t,\,20)",
    "columns_required": ["close"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": (
        "Bali et al. (2011) MAX anomaly, sign-flipped. Higher = no extreme "
        "single-day gain in the past 20 sessions. Strong behavioral premium "
        "in retail-heavy A-share themes; complements gtja191_164."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return inverted 20d MAX daily return (higher = less lottery-like).

    Args:
        panel: Wide OHLCV frames; needs close.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    ret = safe_div(close - close.shift(1), close.shift(1))
    max_ret = ts_max(ret, 20)
    return -max_ret
