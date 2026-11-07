"""Sector "wind" (market tailwind) detector for A-shares.

Research-only. Turns member-level daily panels into a per-day sector state
(dormant / launch / main / divergence / fade) and a continuous wind score, and
provides a validator that checks whether the score predicts *tradable*
forward excess returns (long-only, T+1 entry), so the detector can be
rejected before any sector-specific factor search is built on top of it.

All inputs are wide DataFrames (index = trade date, columns = member codes).
Everything at date t uses data up to and including t only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

LIMIT_UP_PCT = 0.097  # main-board 10% limit, with rounding tolerance

STATES = ("dormant", "launch", "main", "divergence", "fade")


@dataclass(frozen=True)
class WindConfig:
    z_window: int = 60
    short: int = 5
    long: int = 20
    breadth_hi: float = 0.6
    breadth_lo: float = 0.4
    score_launch: float = 0.5
    score_main: float = 1.0


def _zscore(s: pd.Series, window: int) -> pd.Series:
    mean = s.rolling(window, min_periods=max(10, window // 3)).mean()
    std = s.rolling(window, min_periods=max(10, window // 3)).std()
    return (s - mean) / std.replace(0, np.nan)


def wind_features(
    close: pd.DataFrame,
    amount: pd.DataFrame,
    market_amount: pd.Series | None = None,
    cfg: WindConfig = WindConfig(),
) -> pd.DataFrame:
    """Daily sector wind features and the composite ``score``."""
    ret = close.pct_change()
    sector_ret = ret.mean(axis=1)
    sector_amt = amount.sum(axis=1)
    share = sector_amt / market_amount.reindex(sector_amt.index) if market_amount is not None else sector_amt

    mom_s = (1 + sector_ret).rolling(cfg.short).apply(np.prod, raw=True) - 1
    mom_l = (1 + sector_ret).rolling(cfg.long).apply(np.prod, raw=True) - 1
    breadth = (close.pct_change(cfg.short) > 0).mean(axis=1)
    limit_up = (ret >= LIMIT_UP_PCT).sum(axis=1)

    flow_z = _zscore(share.rolling(cfg.short).mean(), cfg.z_window)
    limit_z = _zscore(limit_up.rolling(cfg.short).sum().astype(float), cfg.z_window)
    mom_z = _zscore(mom_s, cfg.z_window)

    score = pd.concat([flow_z, limit_z, mom_z], axis=1).mean(axis=1)
    return pd.DataFrame(
        {
            "mom_short": mom_s,
            "mom_long": mom_l,
            "breadth": breadth,
            "limit_up": limit_up,
            "flow_z": flow_z,
            "score": score,
        }
    )


def classify_state(feat: pd.DataFrame, cfg: WindConfig = WindConfig()) -> pd.Series:
    """Map features to a state; only launch/main are actionable for long-only."""
    score, prev = feat["score"], feat["score"].shift(1)
    rising = score > prev
    state = pd.Series("dormant", index=feat.index, dtype=object)

    launch = (score >= cfg.score_launch) & (score < cfg.score_main) & rising & (feat["mom_long"] > 0)
    main = (score >= cfg.score_main) & (feat["breadth"] >= cfg.breadth_hi) & rising
    diverge = (score >= cfg.score_launch) & ~main & ~launch
    fade = (score < cfg.score_launch) & (feat["mom_long"] < 0) & (prev >= cfg.score_launch)

    state[diverge] = "divergence"
    state[fade] = "fade"
    state[launch] = "launch"
    state[main] = "main"
    state[score.isna()] = "dormant"
    return state


def detect_sector_wind(
    close: pd.DataFrame,
    amount: pd.DataFrame,
    market_amount: pd.Series | None = None,
    cfg: WindConfig = WindConfig(),
) -> pd.DataFrame:
    feat = wind_features(close, amount, market_amount, cfg)
    feat["state"] = classify_state(feat, cfg)
    return feat


def validate_wind_predictiveness(
    score: pd.Series,
    sector_open: pd.Series,
    benchmark_open: pd.Series,
    horizon: int = 5,
    min_obs: int = 30,
) -> dict:
    """Does the score at close of t predict excess return from open(t+1)
    to open(t+1+horizon)? (T+1 executable, long-only.)

    Returns Spearman IC, hit rate of the top-quintile score days and the mean
    forward excess return of top-quintile vs. all days.
    """
    entry = sector_open.shift(-1)
    exit_ = sector_open.shift(-1 - horizon)
    b_entry = benchmark_open.shift(-1)
    b_exit = benchmark_open.shift(-1 - horizon)
    fwd = (exit_ / entry - 1) - (b_exit / b_entry - 1)

    df = pd.concat([score.rename("s"), fwd.rename("f")], axis=1).dropna()
    if len(df) < min_obs:
        return {"n": len(df), "ic": float("nan"), "top_mean": float("nan"),
                "all_mean": float("nan"), "top_hit": float("nan"), "passed": False}

    ic = float(df["s"].corr(df["f"], method="spearman"))
    top = df[df["s"] >= df["s"].quantile(0.8)]
    top_mean = float(top["f"].mean())
    all_mean = float(df["f"].mean())
    top_hit = float((top["f"] > 0).mean())
    return {
        "n": int(len(df)),
        "ic": ic,
        "top_mean": top_mean,
        "all_mean": all_mean,
        "top_hit": top_hit,
        # overlapping windows inflate n, so require a clear edge, not just ic > 0
        "passed": bool(ic > 0.03 and top_mean > all_mean and top_hit > 0.5),
    }
