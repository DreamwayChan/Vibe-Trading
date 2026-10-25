# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 收益率-量变化相关性 60日
# 简要说明: [已修复/sign_flip] ts_corr(close/close_prev, log(v/v_prev), 60)，60日收益率与成交量变化率的相关系数。
# 典型用途: 衡量60日价格变动与成交量变动的相关性，反映量价配合程度。
# ============================================================
"""qlib158 CORD60: formula = \\mathrm{ts\\_corr}(\\mathrm{close}/\\mathrm{close}_{{-1}}, \\log((\\mathrm{volume}+1)/(\\mathrm{volume}_{{-1}}+1)), 60)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from src.factors.base import safe_div, ts_corr

__alpha_meta__ = {
    'id': 'qlib158_cord60',
    'theme': ['volume', 'microstructure'],
    'formula_latex': '\\\\mathrm{ts\\\\_corr}(\\\\mathrm{close}/\\\\mathrm{close}_{{-1}}, \\\\log((\\\\mathrm{volume}+1)/(\\\\mathrm{volume}_{{-1}}+1)), 60)',
    'columns_required': ['close', 'volume'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 60,
    'min_warmup_bars': 60,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0443); applied sign_flip → alive (ic_mean=0.0443, ic+=0.567, ir=0.171). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 CORD60 on the supplied OHLCV panel."""
    c = panel['close']
    v = panel['volume']
    c_ret = safe_div(c, c.shift(1))
    v_ret = safe_div(v + 1.0, v.shift(1) + 1.0)
    logvr = np.log(v_ret)
    return ts_corr(c_ret, logvr, 60)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
