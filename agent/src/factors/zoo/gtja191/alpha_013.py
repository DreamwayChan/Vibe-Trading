
# ============================================================
# 中文名称: GTJA #13 - 开盘涨幅
# 简要说明: [已修复/sign_flip] (-1 * rank( covariance(rank(close), rank(volume), 5))) ，收盘价与成交量5日协方差排名取负。
# 典型用途: 价格与成交量的协同性度量，负值表示量价背离。
# ============================================================
"""GTJA Alpha #13.

Formula: (((HIGH*LOW)^0.5) - VWAP)
Source: 国泰君安 191 alpha 研报 (2014), alpha 13."""

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
    "id": "gtja191_013",
    "theme": ['microstructure'],
    "formula_latex": '(((HIGH*LOW)^0.5) - VWAP)',
    "columns_required": ['high', 'low', 'volume', 'amount'],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 1,
    "min_warmup_bars": 1,
    "notes": 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0364); applied sign_flip → alive (ic_mean=0.0364, ic+=0.572, ir=0.121). Original formula preserved in _compute_raw.',
}

def _compute_raw(panel: dict) -> pd.DataFrame:
    h = panel["high"]
    l = panel["low"]
    v = panel["volume"]
    vw = safe_div(panel["amount"], v * 100.0 + 1.0)
    geo = signed_power(h * l, 0.5)
    return geo - vw


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
