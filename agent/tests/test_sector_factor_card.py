import numpy as np
import pandas as pd

from src.sector_pick.factor_card import build_sector_cards, decay_check


def _bars(n=400, m=20, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-01", periods=n)
    drift = rng.normal(0, 0.002, m)
    ret = rng.normal(0, 0.01, (n, m)) + drift
    close = pd.DataFrame(100 * np.cumprod(1 + ret, axis=0), index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    good = pd.DataFrame(np.tile(drift, (n, 1)), index=idx)
    return idx, close, open_, good


def test_not_actionable_wind_yields_no_cards():
    idx, close, open_, good = _bars()
    data = {str(i): pd.DataFrame({"open": open_[i], "close": close[i], "volume": 1.0}, index=idx)
            for i in close.columns}
    bench = pd.DataFrame({"open": 100.0, "close": 100.0, "volume": 1000.0}, index=idx)
    out = build_sector_cards("t", data, bench, {"good": good})
    if not out["wind"].get("actionable"):
        assert out["cards"] == []


def test_decay_check_retires_inverted_factor():
    idx, close, open_, good = _bars()
    assert not decay_check(good, open_, close)["retire"]
    assert decay_check(-good, open_, close)["retire"]


def test_actionable_wind_emits_card(monkeypatch):
    import src.sector_pick.factor_card as fc

    monkeypatch.setattr(fc, "evaluate_sector_wind", lambda *a, **k: {"actionable": True, "state": "main"})
    idx, close, open_, good = _bars()
    data = {str(i): pd.DataFrame({"open": open_[i], "close": close[i]}, index=idx) for i in close.columns}
    good.columns = [str(c) for c in good.columns]
    out = fc.build_sector_cards("t", data, pd.DataFrame(), {"good": good})
    assert [c["factor"] for c in out["cards"]] == ["good"]
    assert out["cards"][0]["retire_when"]["rolling_ic_window"] == 20
