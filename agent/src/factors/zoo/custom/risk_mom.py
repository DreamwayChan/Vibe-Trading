# ============================================================
# 中文名称: 风险调整动量 (RISK_MOM)
# 简要说明: 近月均收益 / 近月波动，单位风险动量。Moskowitz et al. 风格。
# 典型用途: 可作主排序，或与 gtja191_164 并列的第二动量腿（构造不同）。
# ============================================================
"""custom_risk_mom — risk-adjusted short momentum (return / vol).

Economic meaning:
    Raw return momentum loads on high-volatility names. Scaling mean return
    by realized volatility (a local Sharpe) prefers *efficient* trends and
    reduces lottery/high-beta contamination. Construction differs from
    gtja191_164 (up-move intensity over range), so the two can combine.

Formula:
    r_t = close_t / close_{t-1} - 1
    factor = ts_mean(r, 20) / ts_std(r, 20)

References:
    Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012).
    "Time series momentum." JFE. (vol-scaling intuition)
    Barroso, P., & Santa-Clara, P. (2015). "Momentum has its moments." JFE.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_mean, ts_std

__alpha_meta__ = {
    "id": "custom_risk_mom",
    "nickname": "risk-adjusted momentum (20d Sharpe-like)",
    "theme": ["momentum", "volatility"],
    "formula_latex": (
        r"\mathrm{ts\_mean}(r_t,20)/\mathrm{ts\_std}(r_t,20)"
    ),
    "columns_required": ["close"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": (
        "20d mean return over 20d realized vol (local Sharpe). Higher = "
        "stronger risk-adjusted trend. Distinct from gtja191_164; can be "
        "primary sort or a second momentum leg. Not a reversal/absorb factor."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return 20d risk-adjusted momentum (mean return / realized vol).

    Args:
        panel: Wide OHLCV frames; needs close.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    ret = safe_div(close - close.shift(1), close.shift(1))
    mu = ts_mean(ret, 20)
    sig = ts_std(ret, 20)
    return safe_div(mu, sig)
