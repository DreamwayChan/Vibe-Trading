
# ============================================================
# 中文名称: GTJA Alpha #70
# 简要说明: [已修复/sign_flip] 国泰君安191短周期交易型alpha因子第70号，详见公式定义。
# 典型用途: 在A股市场经中性化处理后用于选股或股指期货日内交易。
# ============================================================
"""GTJA Alpha #70.

Formula: STD(AMOUNT,6)
Source: 国泰君安 191 alpha 研报 (2014), alpha 70."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.factors.base import (
    decay_linear,
    delta,
    rank,
    safe_div,
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

__alpha_meta__ = {
    "id": "gtja191_070",
    "theme": ['volatility', 'volume'],
    "formula_latex": 'STD(AMOUNT,6)',
    "columns_required": ['amount'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 6,
    "min_warmup_bars": 7,
    "notes": 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0267); applied sign_flip → alive (ic_mean=0.0267, ic+=0.550, ir=0.107). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    return ts_std(panel["amount"], 6)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
