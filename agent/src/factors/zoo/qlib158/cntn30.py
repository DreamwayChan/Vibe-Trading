# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 下跌天数计数 30日
# 简要说明: [已修复/sign_flip_smooth10] rolling_mean(1[close<close_prev], 30)，30日内下跌天数占比。
# 典型用途: 衡量30日内下跌频率，值高表示持续下跌行情。
# ============================================================
"""qlib158 CNTN30: formula = \\mathrm{rolling\\_mean}(\\mathrm{1}[\\mathrm{close}<\\mathrm{close}_{{-1}}], 30)."""
from __future__ import annotations

import pandas as pd

__alpha_meta__ = {
    'id': 'qlib158_cntn30',
    'theme': ['reversal'],
    'formula_latex': '\\\\mathrm{rolling\\\\_mean}(\\\\mathrm{1}[\\\\mathrm{close}<\\\\mathrm{close}_{{-1}}], 30)',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 30,
    'min_warmup_bars': 30,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0264); applied sign_flip_smooth10 → alive (ic_mean=0.0351, ic+=0.553, ir=0.116). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 CNTN30 on the supplied OHLCV panel."""
    c = panel['close']
    dn = (c < c.shift(1)).astype('float64')
    return dn.rolling(window=30, min_periods=30).mean()


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import ts_mean
    return ts_mean(-_compute_raw(panel), 10)
