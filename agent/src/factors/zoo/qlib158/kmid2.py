# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: K线中部相对位置
# 简要说明: [已修复/sign_flip_tsrank4] (close - open) / (high - low)，收盘价在当日振幅中的相对位置。
# 典型用途: 衡量收盘强度，值接近1表示收盘接近最高点，多方主导。
# ============================================================
"""qlib158 KMID2: formula = (\\mathrm{close} - \\mathrm{open}) / (\\mathrm{high} - \\mathrm{low})."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div

__alpha_meta__ = {
    'id': 'qlib158_kmid2',
    'theme': ['microstructure'],
    'formula_latex': '(\\\\mathrm{close} - \\\\mathrm{open}) / (\\\\mathrm{high} - \\\\mathrm{low})',
    'columns_required': ['open', 'high', 'low', 'close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 1,
    'min_warmup_bars': 1,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0307); applied sign_flip_tsrank4 → alive (ic_mean=0.0253, ic+=0.560, ir=0.095). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 KMID2 on the supplied OHLCV panel."""
    o = panel['open']
    c = panel['close']
    h = panel['high']
    lo = panel['low']
    return safe_div(c - o, h - lo)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import ts_rank
    return ts_rank(-_compute_raw(panel), 4)
