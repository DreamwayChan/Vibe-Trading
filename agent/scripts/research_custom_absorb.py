"""Research clean rewrites of THS-style "主力吸货" ideas.

Economic targets (not a formula dump):
1. Low-zone absorption strength — near medium-term lows, selling on lows fades
   and closes reclaim the bar.
2. Relative position after range compression — stochastic location when the
   short range has coiled vs a longer range.

Uses the BK1184 run OHLCV artifacts as the investable panel (same universe as
the C-structure prompt). Compares candidates on:
- Rank IC (fwd 1d / 5d / 10d)
- 5-group layered NAV spread
- Cross-sectional Spearman corr vs gtja191_164 (complementarity)

Run from repo ``agent/``:
    python scripts/research_custom_absorb.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Allow ``from src...`` when launched as a script.
_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from src.factors.base import safe_div, ts_max, ts_min  # noqa: E402
from src.factors.factor_analysis_core import (  # noqa: E402
    compute_group_equity,
    compute_ic_series,
)
from src.factors.registry import Registry  # noqa: E402

ARTIFACTS = (
    _AGENT_ROOT
    / "runs"
    / "20260725_094658_01_531d4f"
    / "artifacts"
)
OUT_DIR = _AGENT_ROOT / "runs" / "_research_custom_absorb"
_OHLCV_RE = re.compile(r"^ohlcv_(.+)\.csv$", re.IGNORECASE)


def _sma(x: pd.DataFrame, n: int, m: int) -> pd.DataFrame:
    """GTJA / TDX SMA(x,n,m) → ewm(alpha=m/n)."""
    return x.ewm(alpha=float(m) / float(n), adjust=False).mean()


def _ema(x: pd.DataFrame, span: int) -> pd.DataFrame:
    return x.ewm(span=span, adjust=False).mean()


def load_panel(artifacts: Path) -> dict[str, pd.DataFrame]:
    """Build a wide OHLCV panel from per-symbol artifact CSVs."""
    frames: dict[str, list[pd.Series]] = {
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "volume": [],
    }
    for path in sorted(artifacts.glob("ohlcv_*.csv")):
        m = _OHLCV_RE.match(path.name)
        if not m:
            continue
        code = m.group(1)
        df = pd.read_csv(path, parse_dates=["trade_date"])
        df = df.set_index("trade_date").sort_index()
        for col in frames:
            if col not in df.columns:
                raise KeyError(f"{path.name} missing column {col}")
            frames[col].append(df[col].rename(code))

    panel = {
        k: pd.concat(v, axis=1).sort_index() for k, v in frames.items()
    }
    # Drop rows/cols that are almost empty so IC has enough names per day.
    close = panel["close"]
    keep_cols = close.columns[close.notna().mean() >= 0.6]
    for k in panel:
        panel[k] = panel[k].loc[:, keep_cols]
    return panel


def fwd_return(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Forward return from t close to t+h close (no lookahead in the factor)."""
    return close.shift(-horizon) / close - 1.0


# ---------------------------------------------------------------------------
# Candidate formulas (clean operators only)
# ---------------------------------------------------------------------------


def cand_near_low_38(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """1 - RSV38: higher when close is near the 38d low."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    return 1.0 - safe_div(c - ts_min(l, 38), ts_max(h, 38) - ts_min(l, 38))


def cand_bar_absorb(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """EMA3 of (close-low)/(high-low): intrabar reclaim from the low."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    return _ema(safe_div(c - l, h - l), 3)


def cand_down_fade(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """1 - downside share of |Δlow| (SMA3,1): lows stop cascading."""
    l = panel["low"]
    chg = l - l.shift(1)
    down = (-chg).clip(lower=0.0)
    mag = chg.abs()
    down_share = safe_div(_sma(down, 3, 1), _sma(mag, 3, 1))
    return 1.0 - down_share


def cand_low_absorb_v1(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Near-low × bar-absorb × down-fade, EMA3 (ungated product)."""
    raw = cand_near_low_38(panel) * cand_bar_absorb(panel) * cand_down_fade(panel)
    return _ema(raw, 3)


def cand_low_absorb_v2(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """v1 gated to lower half of 38d range (closer to original VAR7 gate)."""
    near = cand_near_low_38(panel)
    raw = near * cand_bar_absorb(panel) * cand_down_fade(panel)
    gated = raw.where(near >= 0.5, 0.0)
    return _ema(gated, 3)


def cand_low_absorb_v3(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """v1 but near-low uses LOW vs 38d range (support zone), not close RSV."""
    h, l = panel["high"], panel["low"]
    near_support = 1.0 - safe_div(l - ts_min(l, 38), ts_max(h, 38) - ts_min(l, 38))
    raw = near_support * cand_bar_absorb(panel) * cand_down_fade(panel)
    return _ema(raw.where(near_support >= 0.5, 0.0), 3)


def cand_slow_stoch_k(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Slow stochastic K: SMA(RSV21, 13, 8) — cleaned VAR8/VAR9."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    rsv = safe_div(c - ts_min(l, 21), ts_max(h, 21) - ts_min(l, 21)) * 100.0
    return _sma(rsv, 13, 8)


def cand_comp_low(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """(1-RSV38) × compression(10/60): low position after coil."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    near_low = 1.0 - safe_div(c - ts_min(l, 38), ts_max(h, 38) - ts_min(l, 38))
    range10 = ts_max(h, 10) - ts_min(l, 10)
    range60 = ts_max(h, 60) - ts_min(l, 60)
    compression = 1.0 - safe_div(range10, range60)
    return near_low * compression.clip(lower=0.0)


def cand_comp_pos(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """RSV21 × compression(10/60): relative position after coil."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    rsv = safe_div(c - ts_min(l, 21), ts_max(h, 21) - ts_min(l, 21))
    range10 = ts_max(h, 10) - ts_min(l, 10)
    range60 = ts_max(h, 60) - ts_min(l, 60)
    compression = 1.0 - safe_div(range10, range60)
    return rsv * compression.clip(lower=0.0)


def cand_kdj_trend(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cleaned '涨跌' line: 3*SMA(RSV27,5,1) - 2*SMA(SMA(...),3,1)."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    rsv = safe_div(c - ts_min(l, 27), ts_max(h, 27) - ts_min(l, 27)) * 100.0
    k = _sma(rsv, 5, 1)
    d = _sma(k, 3, 1)
    return 3.0 * k - 2.0 * d


def cand_mfm_near_low(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Money-flow multiplier × near-low, SMA6 — volume-free accumulation proxy."""
    c, h, l = panel["close"], panel["high"], panel["low"]
    mfm = safe_div((c - l) - (h - c), h - l)
    near_low = 1.0 - safe_div(c - ts_min(l, 38), ts_max(h, 38) - ts_min(l, 38))
    return _sma(mfm * near_low, 6, 1)


CANDIDATES = {
    "near_low_38": cand_near_low_38,
    "bar_absorb": cand_bar_absorb,
    "down_fade": cand_down_fade,
    "low_absorb_v1": cand_low_absorb_v1,
    "low_absorb_v2": cand_low_absorb_v2,
    "low_absorb_v3": cand_low_absorb_v3,
    "slow_stoch_k": cand_slow_stoch_k,
    "comp_low": cand_comp_low,
    "comp_pos": cand_comp_pos,
    "kdj_trend": cand_kdj_trend,
    "mfm_near_low": cand_mfm_near_low,
}


def _ic_summary(ic: pd.Series) -> dict[str, float]:
    if ic.empty:
        return {
            "ic_mean": float("nan"),
            "ic_std": float("nan"),
            "ir": float("nan"),
            "ic_pos_ratio": float("nan"),
            "n_days": 0,
        }
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) if len(ic) > 1 else float("nan")
    ir = mean / std if std and np.isfinite(std) and std > 0 else float("nan")
    return {
        "ic_mean": mean,
        "ic_std": std,
        "ir": ir,
        "ic_pos_ratio": float((ic > 0).mean()),
        "n_days": int(len(ic)),
    }


def _group_spread(equity: pd.DataFrame) -> dict[str, float]:
    if equity.empty or equity.shape[1] < 2:
        return {"long_short_spread": float("nan"), "mono_score": float("nan")}
    last = equity.iloc[-1]
    g1 = float(last.iloc[0])
    gn = float(last.iloc[-1])
    # Spearman of group index vs terminal NAV — monotonicity proxy.
    # Rank + Pearson avoids a scipy dependency in this research script.
    order = pd.Series(np.arange(1, len(last) + 1, dtype=float), index=last.index)
    mono = float(order.rank().corr(last.rank(), method="pearson"))
    return {"long_short_spread": gn - g1, "mono_score": mono}


def _xs_corr_mean(a: pd.DataFrame, b: pd.DataFrame) -> float:
    """Mean daily cross-sectional Spearman correlation."""
    common_dates = a.index.intersection(b.index)
    common_codes = a.columns.intersection(b.columns)
    if len(common_dates) == 0 or len(common_codes) == 0:
        return float("nan")
    aa = a.loc[common_dates, common_codes]
    bb = b.loc[common_dates, common_codes]
    mask = aa.notna() & bb.notna()
    n = mask.sum(axis=1)
    ar = aa.where(mask).rank(axis=1, method="average")
    br = bb.where(mask).rank(axis=1, method="average")
    corr = ar.corrwith(br, axis=1, method="pearson")
    corr = corr[n >= 5].dropna()
    if corr.empty:
        return float("nan")
    return float(corr.mean())


def main() -> int:
    if not ARTIFACTS.is_dir():
        print(f"missing artifacts dir: {ARTIFACTS}", file=sys.stderr)
        return 1

    print(f"loading panel from {ARTIFACTS} ...")
    panel = load_panel(ARTIFACTS)
    n_names = panel["close"].shape[1]
    n_days = panel["close"].shape[0]
    print(f"panel: {n_days} days × {n_names} names")

    registry = Registry()
    f164 = registry.compute("gtja191_164", panel)

    horizons = (1, 5, 10)
    returns = {h: fwd_return(panel["close"], h) for h in horizons}

    rows: list[dict] = []
    for name, fn in CANDIDATES.items():
        factor = fn(panel)
        # Drop warmup / all-nan edge.
        row: dict = {"candidate": name}
        for h in horizons:
            ic = compute_ic_series(factor, returns[h])
            summary = _ic_summary(ic)
            for k, v in summary.items():
                row[f"h{h}_{k}"] = v
            if h == 5:
                equity = compute_group_equity(factor, returns[h], n_groups=5)
                row.update({f"h5_{k}": v for k, v in _group_spread(equity).items()})
        row["corr_gtja191_164"] = _xs_corr_mean(factor, f164)
        # Complementarity score: prefer |IC| with low |corr| to 164.
        ic5 = row.get("h5_ic_mean", float("nan"))
        corr = row.get("corr_gtja191_164", float("nan"))
        if np.isfinite(ic5) and np.isfinite(corr):
            row["complement_score"] = abs(ic5) * (1.0 - min(abs(corr), 1.0))
        else:
            row["complement_score"] = float("nan")
        rows.append(row)
        print(
            f"{name:16s}  IC5={ic5:+.4f}  IR5={row.get('h5_ir', float('nan')):+.3f}  "
            f"spread5={row.get('h5_long_short_spread', float('nan')):+.3f}  "
            f"corr164={corr:+.3f}  comp={row['complement_score']:.4f}"
        )

    # Also bench the existing momentum anchor.
    row164: dict = {"candidate": "gtja191_164"}
    for h in horizons:
        ic = compute_ic_series(f164, returns[h])
        summary = _ic_summary(ic)
        for k, v in summary.items():
            row164[f"h{h}_{k}"] = v
        if h == 5:
            equity = compute_group_equity(f164, returns[h], n_groups=5)
            row164.update({f"h5_{k}": v for k, v in _group_spread(equity).items()})
    row164["corr_gtja191_164"] = 1.0
    row164["complement_score"] = 0.0
    rows.append(row164)

    result = pd.DataFrame(rows).sort_values("complement_score", ascending=False)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "candidate_rank.csv"
    json_path = OUT_DIR / "candidate_rank.json"
    result.to_csv(csv_path, index=False)
    json_path.write_text(
        json.dumps(result.to_dict(orient="records"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("\n=== ranked by complement_score (abs IC5 × (1-|corr164|)) ===")
    cols = [
        "candidate",
        "h1_ic_mean",
        "h5_ic_mean",
        "h5_ir",
        "h5_ic_pos_ratio",
        "h5_long_short_spread",
        "h5_mono_score",
        "h10_ic_mean",
        "corr_gtja191_164",
        "complement_score",
    ]
    print(result[cols].to_string(index=False, float_format=lambda x: f"{x:+.4f}"))
    print(f"\nwrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
