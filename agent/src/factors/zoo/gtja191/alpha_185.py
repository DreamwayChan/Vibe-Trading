
# ============================================================
# 中文名称: GTJA Alpha #185
# 简要说明: [已修复/smooth5_rank] 国泰君安191短周期交易型alpha因子第185号，详见公式定义。
# 典型用途: 在A股市场经中性化处理后用于选股或股指期货日内交易。
# ============================================================
"""GTJA Alpha 185 (国泰君安 191 短周期交易型 alpha 因子, 2014).

Formula (verbatim from the report):
    RANK((-1*((1-(OPEN/CLOSE))^2)))

Notes: 
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
)

ALPHA_ID = "gtja191_185"

__alpha_meta__ = {
    'id': 'gtja191_185',
    'theme': ['reversal'],
    'formula_latex': 'rank(-1*(1-open/close)^2)',
    'columns_required': ['open', 'close'],
    'extras_required': [],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 1,
    'min_warmup_bars': 1,
    'notes': 'REPAIR CSI300 2023-2026: original dead (ic_mean=0.0237); applied smooth5_rank → alive (ic_mean=0.0494, ic+=0.580, ir=0.202). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel):
    """Compute gtja191_185.

    Args:
        panel: dict[str, pd.DataFrame] with at least the required columns.

    Returns:
        pd.DataFrame with index = panel["close"].index, columns = panel["close"].columns.
    """
    c = panel["close"]
    o = panel["open"]
    out = rank(-1.0 * (1.0 - safe_div(o, c)) ** 2)
    return out


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import rank, ts_mean
    return rank(ts_mean(_compute_raw(panel), 5))
