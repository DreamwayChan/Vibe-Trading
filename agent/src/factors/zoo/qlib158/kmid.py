# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 涨跌幅
# 简要说明: [已修复/sign_flip] (close - open) / open，即当日收盘相对于开盘的简单收益率。
# 典型用途: 日内动量因子，正值为多头占优，负值为空头占优。
# ============================================================
"""qlib158 KMID: formula = (\\mathrm{close} - \\mathrm{open}) / \\mathrm{open}."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div

__alpha_meta__ = {
    'id': 'qlib158_kmid',
    'theme': ['microstructure'],
    'formula_latex': '(\\\\mathrm{close} - \\\\mathrm{open}) / \\\\mathrm{open}',
    'columns_required': ['open', 'close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 1,
    'min_warmup_bars': 1,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0307); applied sign_flip → alive (ic_mean=0.0307, ic+=0.559, ir=0.107). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 KMID on the supplied OHLCV panel."""
    o = panel['open']
    c = panel['close']
    return safe_div(c - o, o)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
