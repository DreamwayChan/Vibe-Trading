
# ============================================================
# 中文名称: GTJA Alpha #156
# 简要说明: [已修复/decay8_rank] 国泰君安191短周期交易型alpha因子第156号，详见公式定义。
# 典型用途: 在A股市场经中性化处理后用于选股或股指期货日内交易。
# ============================================================
"""GTJA Alpha 156 (国泰君安 191 短周期交易型 alpha 因子, 2014).

Formula (verbatim from the report):
    MAX(RANK(DECAYLINEAR(DELTA(VWAP,5),3)), RANK(DECAYLINEAR((-1*((OPEN*0.15)+(LOW*0.85)) * DELTA(...,2)/((OPEN*0.15)+(LOW*0.85))),3)))*-1

Notes: MAX elementwise -> np.maximum.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.factors.base import (
    decay_linear,
    delta,
    rank,
    safe_div,
    scale,
    signed_power,
    ts_argmax,
    ts_argmin,
    ts_corr,
    ts_cov,
    ts_max,
    ts_mean,
    ts_min,
    ts_rank,
    ts_std,
    vwap,
)

ALPHA_ID = "gtja191_156"

__alpha_meta__ = {
    'id': 'gtja191_156',
    'theme': ['volume'],
    'formula_latex': 'see body',
    'columns_required': ['open', 'high', 'low', 'close', 'volume', 'amount'],
    'extras_required': [],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 10,
    'notes': 'REPAIR CSI300 2023-2026: original dead (ic_mean=0.0169); applied decay8_rank → alive (ic_mean=0.0304, ic+=0.582, ir=0.126). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel):
    """Compute gtja191_156.

    Args:
        panel: dict[str, pd.DataFrame] with at least the required columns.

    Returns:
        pd.DataFrame with index = panel["close"].index, columns = panel["close"].columns.
    """
    o = panel["open"]
    l = panel["low"]
    vw = vwap(panel, "equity_cn")

    a = rank(decay_linear(delta(vw, 5), 3))
    mix = o * 0.15 + l * 0.85
    b_inner = -1.0 * safe_div(delta(mix, 2), mix)
    b = rank(decay_linear(b_inner, 3))
    arr = np.maximum(a.to_numpy(dtype=np.float64, na_value=np.nan),
                     b.to_numpy(dtype=np.float64, na_value=np.nan))
    out = pd.DataFrame(arr, index=a.index, columns=a.columns) * -1.0
    return out


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import rank, decay_linear
    return rank(decay_linear(_compute_raw(panel), 8))
