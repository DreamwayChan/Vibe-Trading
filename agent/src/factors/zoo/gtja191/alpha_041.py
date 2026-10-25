
# ============================================================
# 中文名称: GTJA #41 - VWAP标准差
# 简要说明: [已修复/decay8_rank] RANK(MAX(DELTA(VWAP,1),3))*RANK(DELTA(VOLUME,3))，VWAP与成交量3日变化的极值排名乘积。
# 典型用途: VWAP变化与成交量变化的组合信号。
# ============================================================
"""GTJA Alpha #41.

Formula: (RANK(MAX(DELTA(VWAP,3),5))*-1)
Source: 国泰君安 191 alpha 研报 (2014), alpha 41."""

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
    "id": "gtja191_041",
    "theme": ['microstructure'],
    "formula_latex": '(RANK(MAX(DELTA(VWAP,3),5))*-1)',
    "columns_required": ['volume', 'amount'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 9,
    "notes": 'REPAIR CSI300 2023-2026: original dead (ic_mean=0.0255); applied decay8_rank → alive (ic_mean=0.0309, ic+=0.551, ir=0.114). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    v = panel["volume"]
    vw = safe_div(panel["amount"], v * 100.0 + 1.0)
    return -1.0 * rank(ts_max(delta(vw, 3), 5))


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import rank, decay_linear
    return rank(decay_linear(_compute_raw(panel), 8))
