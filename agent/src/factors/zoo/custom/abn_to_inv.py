# ============================================================
# 中文名称: 逆异常换手 (ABN_TO_INV)
# 简要说明: 短期成交相对中期异常放大越小越好（低换手/去投机溢价）。Datar et al.(1998)。
# 典型用途: 动量确认——剔除短期放量追逐的拥挤标的。
# ============================================================
"""custom_abn_to_inv — inverted abnormal turnover (Datar / China low-TO).

Economic meaning:
    High turnover stocks underperform in many markets; the premium is
    especially documented in China A-shares where retail churn is large.
    Without float shares we proxy turnover by volume and measure *abnormal*
    turnover: short-window volume vs a longer baseline. Prefer names that
    are *not* in a short-term volume spike.

Formula:
    abn_to = ts_mean(volume, 5) / ts_mean(volume, 60)
    factor = -abn_to               (higher = quieter relative activity)

References:
    Datar, V. T., Naik, N. Y., & Radcliffe, R. (1998).
    "Liquidity and stock returns: An alternative test." JFM.
    Liu, J., Stambaugh, R. F., & Yuan, Y. (2019). China factor evidence.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_mean

__alpha_meta__ = {
    "id": "custom_abn_to_inv",
    "nickname": "inverted abnormal turnover (5d/60d)",
    "theme": ["liquidity", "volume", "sentiment"],
    "formula_latex": (
        r"-\,\mathrm{ts\_mean}(\mathrm{volume},5)"
        r"/\mathrm{ts\_mean}(\mathrm{volume},60)"
    ),
    "columns_required": ["volume"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 60,
    "min_warmup_bars": 60,
    "notes": (
        "Turnover premium proxy without float shares: abnormal volume "
        "5d/60d, sign-flipped. Higher = less short-term volume spike. "
        "Use as momentum confirm to avoid crowded retail chase."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return inverted abnormal turnover (higher = quieter relative volume).

    Args:
        panel: Wide OHLCV frames; needs volume.

    Returns:
        Factor panel aligned to ``panel["volume"]``.
    """
    volume = panel["volume"]
    # Short vs long volume baseline — spike detection without share float.
    abn_to = safe_div(ts_mean(volume, 5), ts_mean(volume, 60))
    return -abn_to
