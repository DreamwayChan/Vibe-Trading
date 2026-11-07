"""In-sector factor selection under A-share long-only, T+1 constraints.

Research-only. Instead of inventing factors, it screens existing library
factors on one sector's panel: forward return is open(t+1) -> open(t+1+h),
names that open at limit-up (unbuyable) are dropped, IC is shrunk toward a
market-wide prior because a sector panel is small, and a factor must keep the
same sign on a later out-of-sample segment to be selected.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

LIMIT_UP_PCT = 0.097


def forward_returns(open_: pd.DataFrame, close: pd.DataFrame, horizon: int = 5) -> pd.DataFrame:
    """Executable long-only forward return; NaN where entry is limit-up at open."""
    entry = open_.shift(-1)
    fwd = open_.shift(-1 - horizon) / entry - 1
    prev_close = close
    gap_up = entry / prev_close - 1 >= LIMIT_UP_PCT
    return fwd.mask(gap_up)


def daily_rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 5) -> pd.Series:
    fr, rr = factor.rank(axis=1), fwd.rank(axis=1)
    valid = (factor.notna() & fwd.notna()).sum(axis=1)
    ic = fr.corrwith(rr, axis=1)
    return ic.where(valid >= min_names).dropna()


@dataclass(frozen=True)
class FactorVerdict:
    name: str
    n: int
    ic_in: float
    ic_out: float
    shrunk_ic: float
    t_out: float
    selected: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def screen_factors(
    factors: dict[str, pd.DataFrame],
    open_: pd.DataFrame,
    close: pd.DataFrame,
    *,
    horizon: int = 5,
    prior_ic: dict[str, float] | None = None,
    prior_weight: int = 60,
    oos_frac: float = 0.4,
    min_t: float = 1.5,
    min_days: int = 60,
) -> list[FactorVerdict]:
    """Rank factors by shrunk IC; select those stable out of sample.

    Overlapping horizons make daily ICs autocorrelated, so the OOS t-stat uses
    a de-overlapped sample (every ``horizon``-th day).
    """
    fwd = forward_returns(open_, close, horizon)
    out: list[FactorVerdict] = []
    for name, f in factors.items():
        f = f.reindex(index=close.index, columns=close.columns)
        ic = daily_rank_ic(f, fwd)
        if len(ic) < min_days:
            out.append(FactorVerdict(name, len(ic), np.nan, np.nan, np.nan, np.nan, False))
            continue
        cut = int(len(ic) * (1 - oos_frac))
        ic_in, ic_out = ic.iloc[:cut], ic.iloc[cut:]
        sample = ic_out.iloc[::horizon]
        std = sample.std()
        t_out = float(sample.mean() / (std / np.sqrt(len(sample)))) if len(sample) > 2 and std > 0 else np.nan

        prior = (prior_ic or {}).get(name, 0.0)
        n_eff = max(len(ic_in) // horizon, 1)
        shrunk = (n_eff * ic_in.mean() + prior_weight / horizon * prior) / (n_eff + prior_weight / horizon)

        selected = bool(
            shrunk > 0
            and ic_out.mean() > 0
            and np.isfinite(t_out)
            and t_out >= min_t
        )
        out.append(FactorVerdict(name, len(ic), float(ic_in.mean()), float(ic_out.mean()),
                                 float(shrunk), t_out, selected))
    return sorted(out, key=lambda v: (not v.selected, -(v.shrunk_ic if np.isfinite(v.shrunk_ic) else -9)))


def screen_registry_factors(
    panel: dict[str, pd.DataFrame],
    alpha_ids: list[str],
    **kwargs,
) -> list[FactorVerdict]:
    """Compute registry alphas on a sector panel and screen them."""
    from src.factors.registry import Registry

    reg = Registry()
    factors: dict[str, pd.DataFrame] = {}
    for aid in alpha_ids:
        try:
            raw = reg.compute(aid, panel)
        except Exception:  # noqa: BLE001 — a factor that cannot compute is skipped
            continue
        if raw is not None and not getattr(raw, "empty", True):
            factors[aid] = raw
    return screen_factors(factors, panel["open"], panel["close"], **kwargs)
