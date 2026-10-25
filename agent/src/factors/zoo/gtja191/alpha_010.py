
# ============================================================
# 中文名称: GTJA #10 - 条件收益平方极值
# 简要说明: [已修复/sign_flip] RANK(MAX(((RET<0)?STD(RET,20):CLOSE)^2,5))，条件收益平方的5日最大值。
# 典型用途: 识别短期剧烈波动后的潜在反转点。
# ============================================================
"""GTJA Alpha #10.

Formula: RANK(MAX(((RET<0)?STD(RET,20):CLOSE)^2,5))
Source: 国泰君安 191 alpha 研报 (2014), alpha 10."""

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
    "id": "gtja191_010",
    "theme": ['volatility', 'reversal'],
    "formula_latex": 'RANK(MAX(((RET<0)?STD(RET,20):CLOSE)^2,5))',
    "columns_required": ['close'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 21,
    "notes": 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0419); applied sign_flip → alive (ic_mean=0.0419, ic+=0.584, ir=0.146). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    c = panel["close"]
    pc = c.shift(1)
    ret = safe_div(c - pc, pc)
    s20 = ts_std(ret, 20)
    pick = ret.copy()
    pick = pick.where(ret < 0, c)
    pick = pick.where(~(ret < 0), s20)
    return rank(ts_max(pick * pick, 5))


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
