
# ============================================================
# 中文名称: GTJA #30 - 开盘相对强度
# 简要说明: [已修复/sign_flip] RANK(DELTA(VWAP,1))^3 * RANK(CORR(LOW,MEAN(VOLUME,50),12))，VWAP变化与量价相关的组合。
# 典型用途: VWAP变化强度与成交量-价格关系的复合指标。
# ============================================================
"""GTJA Alpha #30.

Formula: WMA((REGRESI(CLOSE/DELAY(CLOSE,1)-1, MKT_RET, SMB, HML, 60))^2, 20)
Source: 国泰君安 191 alpha 研报 (2014), alpha 30."""

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
    "id": "gtja191_030",
    "theme": ['volatility'],
    "formula_latex": 'WMA((REGRESI(CLOSE/DELAY(CLOSE,1)-1, MKT_RET, SMB, HML, 60))^2, 20)',
    "columns_required": ['close'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0422); applied sign_flip → alive (ic_mean=0.0422, ic+=0.565, ir=0.147). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    c = panel["close"]
    pc = c.shift(1)
    ret = safe_div(c - pc, pc)
    return decay_linear(ret * ret, 20)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
