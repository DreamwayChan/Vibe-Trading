# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 价格标准差比 5日
# 简要说明: [已修复/sign_flip] ts_std(close, 5) / close，5日收盘价标准差与收盘价的比率（变异系数）。
# 典型用途: 衡量5日价格波动幅度相对于价格水平的比率，用于波动率排序。
# ============================================================
"""qlib158 STD5: formula = \\mathrm{ts\\_std}(\\mathrm{close}, 5) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div, ts_std

__alpha_meta__ = {
    'id': 'qlib158_std5',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_std}(\\\\mathrm{close}, 5) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 5,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0395); applied sign_flip → alive (ic_mean=0.0395, ic+=0.560, ir=0.151). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 STD5 on the supplied OHLCV panel."""
    c = panel['close']
    return safe_div(ts_std(c, 5), c)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
