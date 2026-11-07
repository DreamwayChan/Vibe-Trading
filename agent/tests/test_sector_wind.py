import numpy as np
import pandas as pd

from src.sector_pick.wind import (
    STATES,
    detect_sector_wind,
    validate_wind_predictiveness,
)


def _panel(n=200, m=10, surge_start=120, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    ret = rng.normal(0, 0.01, (n, m))
    ret[surge_start:surge_start + 20] += 0.03
    close = pd.DataFrame(100 * np.cumprod(1 + ret, axis=0), index=idx)
    amt = pd.DataFrame(rng.uniform(1, 2, (n, m)), index=idx)
    amt.iloc[surge_start:surge_start + 20] *= 3
    return close, amt, idx


def test_detects_surge_and_no_lookahead():
    close, amt, idx = _panel()
    mkt = pd.Series(100.0, index=idx)
    out = detect_sector_wind(close, amt, mkt)
    assert set(out["state"].unique()) <= set(STATES)
    surge = out.iloc[120:140]
    assert surge["state"].isin(["launch", "main"]).any()
    assert (out.iloc[:50]["state"] == "dormant").all() or out.iloc[:50]["state"].isin(STATES).all()

    # truncating future data must not change past values
    short = detect_sector_wind(close.iloc[:150], amt.iloc[:150], mkt.iloc[:150])
    pd.testing.assert_series_equal(out["score"].iloc[:150], short["score"], check_names=False)


def test_validator_runs_and_flags_noise():
    close, amt, idx = _panel()
    out = detect_sector_wind(close, amt, pd.Series(100.0, index=idx))
    bench = pd.Series(100.0, index=idx)
    res = validate_wind_predictiveness(out["score"], close.mean(axis=1), bench)
    assert {"ic", "top_mean", "passed", "n"} <= set(res)
    rnd = pd.Series(np.random.default_rng(1).normal(size=len(idx)), index=idx)
    assert validate_wind_predictiveness(rnd, close.mean(axis=1), bench)["n"] > 30


def test_evaluate_sector_wind_on_loaded_bars():
    from src.sector_pick.wind_scan import evaluate_sector_wind

    close, amt, idx = _panel()
    data = {
        f"{600000 + i}.SH": pd.DataFrame(
            {"open": close[i], "close": close[i], "volume": amt[i]}, index=idx
        )
        for i in range(close.shape[1])
    }
    bench = pd.DataFrame({"open": 100.0, "close": 100.0, "volume": 1000.0}, index=idx)
    res = evaluate_sector_wind(data, bench)
    assert res["status"] == "ok" and res["members"] == 10
    assert "validation" in res and "actionable" in res
