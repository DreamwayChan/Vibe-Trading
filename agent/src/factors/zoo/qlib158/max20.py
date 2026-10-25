# Adapted from microsoft/qlib@d5379c520f66a39953bad76234a7019a72796fd0:qlib/contrib/data/handler.py
# (Apache-2.0). Copyright (c) Microsoft Corporation.
# ============================================================
# 中文名称: 最大价 20日
# 简要说明: [已修复/sign_flip_smooth10] ts_max(high, 20) / close，20日最高价与当前收盘价的比率。
# 典型用途: 衡量当前价格相对于20日最高点的位置，接近1表示接近近期高点。
# ============================================================
"""qlib158 MAX20: formula = \\mathrm{ts\\_max}(\\mathrm{high}, 20) / \\mathrm{close}."""
from __future__ import annotations

import pandas as pd
from src.factors.base import safe_div, ts_max

__alpha_meta__ = {
    'id': 'qlib158_max20',
    'theme': ['momentum'],
    'formula_latex': '\\\\mathrm{ts\\\\_max}(\\\\mathrm{high}, 20) / \\\\mathrm{close}',
    'columns_required': ['high', 'close'],
    'universe': ['equity_us', 'equity_cn', 'equity_hk', 'equity_in'],
    'frequency': ['1d'],
    'decay_horizon': 20,
    'min_warmup_bars': 20,
    'notes': 'REPAIR CSI300 2023-2026: original reversed (ic_mean=-0.0311); applied sign_flip_smooth10 → alive (ic_mean=0.0414, ic+=0.563, ir=0.134). Original formula preserved in _compute_raw.',
}


def _compute_raw(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return qlib158 MAX20 on the supplied OHLCV panel."""
    h = panel['high']
    c = panel['close']
    return safe_div(ts_max(h, 20), c)


def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    from src.factors.base import ts_mean
    return ts_mean(-_compute_raw(panel), 10)
