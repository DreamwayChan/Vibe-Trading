# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: K线长度
# 简要说明: [已修复/sign_flip] (high - low) / open，衡量当日K线实体振幅相对于开盘价的比率。
# 典型用途: 用于识别日内波动幅度较大的股票，结合其他因子判断异常交易行为。
# ============================================================
"""qlib158 KLEN: formula = (\\mathrm{high} - \\mathrm{low}) / \\mathrm{open}."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div

__alpha_meta__ = {
    'id': 'qlib158_klen',
    'theme': ['microstructure'],
    'formula_latex': '(\\\\mathrm{high} - \\\\mathrm{low}) / \\\\mathrm{open}',
    'columns_required': ['open', 'high', 'low'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 1,
    'min_warmup_bars': 1,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0420); applied sign_flip → alive (ic_mean=0.0420, ic+=0.576, ir=0.169). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 KLEN on the supplied OHLCV panel."""
    o = panel['open']
    h = panel['high']
    lo = panel['low']
    return safe_div(h - lo, o)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    return -_compute_raw(panel)
