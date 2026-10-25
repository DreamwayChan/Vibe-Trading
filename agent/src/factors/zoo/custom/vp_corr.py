# ============================================================
# 中文名称: 量价同向确认 (VP_CORR)
# 简要说明: 收益与成交量滚动相关越高，趋势越健康（量价配合）。
# 典型用途: 动量确认——价格上行需有成交配合，过滤无量虚拉。
# ============================================================
"""custom_vp_corr — rolling return–volume correlation (trend quality).

Economic meaning:
    Healthy intermediate trends tend to show positive association between
    daily returns and trading activity (volume rises on up days). A high
    rolling correlation is a microstructure / flow *confirm* for price
    momentum — complementary to pure range-location "absorb" charts.

Formula:
    r_t = close_t / close_{t-1} - 1
    factor = ts_corr(r, volume, 20)

References:
    Classic volume-price confirmation (Blume-Easley-O'Hara style intuition);
    used here as a short-horizon confirm, not a Level-2 money-flow claim.
"""

from __future__ import annotations

import pandas as pd

from src.factors.base import safe_div, ts_corr

__alpha_meta__ = {
    "id": "custom_vp_corr",
    "nickname": "return-volume correlation (20d)",
    "theme": ["volume", "momentum", "microstructure"],
    "formula_latex": r"\mathrm{ts\_corr}(r_t,\mathrm{volume}_t,\,20)",
    "columns_required": ["close", "volume"],
    "extras_required": [],
    "requires_sector": False,
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 21,
    "notes": (
        "20d Pearson corr of daily return vs volume. Higher = price moves "
        "align with activity (healthier trend). Chart-only absorb proxies "
        "replaced this role poorly; keep as confirm next to gtja191_164."
    ),
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Return 20d correlation between daily return and volume.

    Args:
        panel: Wide OHLCV frames; needs close and volume.

    Returns:
        Factor panel aligned to ``panel["close"]``.
    """
    close = panel["close"]
    volume = panel["volume"]
    ret = safe_div(close - close.shift(1), close.shift(1))
    return ts_corr(ret, volume, 20)
