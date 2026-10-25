# ============================================================
# 中文名称: 逆特质波动 (IVOL_INV)
# 简要说明: 相对等权市场收益的残差波动越低越好（低 IVOL 溢价）。Ang et al.(2006)。
# 典型用途: 动量主排序的质量确认——过滤高噪音/高投机标的。
# ============================================================
"""custom_ivol_inv — inverted idiosyncratic volatility (Ang et al. 2006).

Economic meaning:
    Stocks with high idiosyncratic volatility earn lower subsequent returns
    (the IVOL anomaly). In China A-shares this is especially strong among
    retail-heavy names. Prefer quieter residual risk as a *confirm* for
    intermediate momentum rather than as a standalone sort.

Formula:
    r_i,t = close_t / close_{t-1} - 1
    r_m,t = equal-weight cross-sectional mean of r_i,t
    e_i,t = r_i,t - r_m,t          (beta≈1 market residual)
    IVOL_i,t = std(e_i, t-19..t)
    factor = -IVOL                 (higher = lower residual risk)

References:
    Ang, A., Hodrick, R. J., Xing, Y., & Zhang, X. (2006).
    "The Cross-Section of Volatility and Expected Returns." JF.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_std

__alpha_meta__ = {
    "id": "custom_ivol_inv",
    "nickname": "inverted idiosyncratic volatility (20d)",
    "theme": ["volatility", "quality"],
    "formula_latex": (
        r"-\,\mathrm{ts\_std}\bigl(r_i-\bar r^{\mathrm{ew}},\,20\bigr)"
    ),
    "columns_required": ["close"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": (
        "Ang et al. (2006) IVOL, sign-flipped so higher = quieter residual. "
        "Uses equal-weight market residual (beta≈1), not a full rolling beta "
        "regression — keeps warmup short for theme-pool weekly rebalance. "
        "Intended as momentum confirm, complementary to gtja191_164."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return -20d idiosyncratic volatility (higher = lower residual risk).

    Args:
        panel: Wide OHLCV frames; needs close.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    # Simple returns; first bar is NaN by construction (no lookahead).
    ret = safe_div(close - close.shift(1), close.shift(1))

    # Equal-weight market proxy broadcast to each column for residualization.
    market_ret = ret.mean(axis=1, skipna=True)
    residual = ret.sub(market_ret, axis=0)

    ivol = ts_std(residual, 20)
    return -ivol
