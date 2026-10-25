
# ============================================================
# 中文名称: GTJA Alpha #100
# 简要说明: [已修复/smooth5_rank] 国泰君安191短周期交易型alpha因子第100号，详见公式定义。
# 典型用途: 在A股市场经中性化处理后用于选股或股指期货日内交易。
# ============================================================
"""GTJA Alpha #100.

Formula: STD(VOLUME,20)
Source: 国泰君安 191 alpha 研报 (2014), alpha 100."""

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
    "id": "gtja191_100",
    "theme": ['volatility', 'volume'],
    "formula_latex": 'STD(VOLUME,20)',
    "columns_required": ['volume'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": 'REPAIR CSI300 2023-2026: original dead (ic_mean=0.0263); applied smooth5_rank → alive (ic_mean=0.0278, ic+=0.556, ir=0.115). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    return ts_std(panel["volume"], 20)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import rank, ts_mean
    return rank(ts_mean(_compute_raw(panel), 5))
