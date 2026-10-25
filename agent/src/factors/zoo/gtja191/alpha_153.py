
# ============================================================
# 中文名称: GTJA Alpha #153
# 简要说明: [已修复/sign_flip] 国泰君安191短周期交易型alpha因子第153号，详见公式定义。
# 典型用途: 在A股市场经中性化处理后用于选股或股指期货日内交易。
# ============================================================
"""GTJA Alpha 153 (国泰君安 191 短周期交易型 alpha 因子, 2014).

Formula (verbatim from the report):
    (MEAN(CLOSE,3)+MEAN(CLOSE,6)+MEAN(CLOSE,12)+MEAN(CLOSE,24))/4

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

ALPHA_ID = "gtja191_153"

__alpha_meta__ = {
    'id': 'gtja191_153',
    'theme': ['momentum'],
    'formula_latex': '(mean(c,3)+mean(c,6)+mean(c,12)+mean(c,24))/4',
    'columns_required': ['close'],
    'extras_required': [],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 24,
    'min_warmup_bars': 24,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0383); applied sign_flip → alive (ic_mean=0.0383, ic+=0.571, ir=0.127). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel):
    """Compute gtja191_153.

    Args:
        panel: dict[str, pd.DataFrame] with at least the required columns.

    Returns:
        pd.DataFrame with index = panel["close"].index, columns = panel["close"].columns.
    """
    c = panel["close"]
    out = (ts_mean(c, 3) + ts_mean(c, 6) + ts_mean(c, 12) + ts_mean(c, 24)) / 4.0
    return out


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
