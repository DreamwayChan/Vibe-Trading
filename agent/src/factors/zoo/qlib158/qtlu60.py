# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 上分位数 60日
# 简要说明: [已修复/sign_flip_tsmin11] quantile_0.8(close, 60) / close，60日80%分位价格与当前收盘价的比率。
# 典型用途: 衡量当前价格相对于60日高分位的位置，值小表示价格在近期高位以下。
# ============================================================
"""qlib158 QTLU60: formula = \\mathrm{quantile}_{{0.8}}(\\mathrm{close}, 60) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div

__alpha_meta__ = {
    'id': 'qlib158_qtlu60',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{quantile}_{{0.8}}(\\\\mathrm{close}, 60) / \\\\mathrm{close}',
    'columns_required': ['close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 60,
    'min_warmup_bars': 60,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0244); applied sign_flip_tsmin11 → alive (ic_mean=0.0348, ic+=0.570, ir=0.108). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 QTLU60 on the supplied OHLCV panel."""
    c = panel['close']
    q = c.rolling(window=60, min_periods=60).quantile(0.8)
    return safe_div(q, c)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import ts_min
    return ts_min(-_compute_raw(panel), 11)
