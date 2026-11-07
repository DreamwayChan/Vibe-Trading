import numpy as np
import pandas as pd

from src.sector_pick.factor_select import forward_returns, screen_factors


def _data(n=400, m=30, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-01", periods=n)
    cols = [f"s{i}" for i in range(m)]
    # persistent per-stock drift => a "drift" factor is genuinely predictive
    drift = rng.normal(0, 0.002, m)
    ret = rng.normal(0, 0.01, (n, m)) + drift
    close = pd.DataFrame(100 * np.cumprod(1 + ret, axis=0), index=idx, columns=cols)
    open_ = close.shift(1).fillna(close.iloc[0])
    good = pd.DataFrame(np.tile(drift, (n, 1)), index=idx, columns=cols)
    noise = pd.DataFrame(rng.normal(size=(n, m)), index=idx, columns=cols)
    return open_, close, good, noise


def test_screen_selects_real_factor_not_noise():
    open_, close, good, noise = _data()
    res = {v.name: v for v in screen_factors({"good": good, "noise": noise}, open_, close)}
    assert res["good"].selected
    assert not res["noise"].selected


def test_limit_up_open_is_excluded():
    open_, close, _, _ = _data(n=50, m=6)
    open_.iloc[11, 0] = close.iloc[10, 0] * 1.10  # t+1 opens limit-up
    fwd = forward_returns(open_, close, 5)
    assert np.isnan(fwd.iloc[10, 0])
    assert not np.isnan(fwd.iloc[10, 1])
