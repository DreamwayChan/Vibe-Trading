"""Diagnose why E quality-confirm underperformed C on BK1184.

Loads the same OHLCV artifacts as the C/E runs and reports:
1. Rank IC / IR of each confirm candidate (fwd 5d / 10d)
2. Cross-sectional Spearman corr vs gtja191_164 (conflict vs complement)
3. How often Top6-by-164 names pass each confirm filter
4. Forward return of names blocked by E but kept by C (opportunity cost)
5. Simple counterfactual filters (threshold / hybrid / soft blend)

Run from ``agent/``:
    python scripts/research_quality_e_diagnosis.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from src.factors.factor_analysis_core import compute_ic_series  # noqa: E402
from src.factors.registry import Registry  # noqa: E402

ARTIFACTS = (
    _AGENT_ROOT / "runs" / "20260725_094658_01_531d4f" / "artifacts"
)
OUT_DIR = _AGENT_ROOT / "runs" / "_research_quality_e_diagnosis"
_OHLCV_RE = re.compile(r"^ohlcv_(.+)\.csv$", re.IGNORECASE)

MOM_ID = "gtja191_164"
C_CONFIRM = ["gtja191_114", "gtja191_171", "gtja191_054"]
E_CONFIRM = [
    "custom_ivol_inv",
    "custom_abn_to_inv",
    "custom_max_inv",
    "custom_vp_corr",
]
EXTRA = ["custom_risk_mom", "custom_comp_low", "custom_near_low"]
ALL_IDS = [MOM_ID] + C_CONFIRM + E_CONFIRM + EXTRA


def load_panel(artifacts: Path) -> dict[str, pd.DataFrame]:
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
            frames[col].append(df[col].rename(code))
    panel = {k: pd.concat(v, axis=1).sort_index() for k, v in frames.items()}
    # Align columns across fields.
    cols = sorted(set.intersection(*(set(df.columns) for df in panel.values())))
    panel = {k: v.reindex(columns=cols) for k, v in panel.items()}
    # gtja191_114 needs amount; artifacts often omit it — approximate CN dollar volume.
    panel["amount"] = panel["close"] * panel["volume"]
    return panel


def smooth_cs(raw: pd.DataFrame, w: int = 5) -> pd.DataFrame:
    cs = raw.rank(axis=1, pct=True, ascending=True)
    return cs.rolling(window=w, min_periods=3).mean()


def weekly_mask(dates: pd.DatetimeIndex) -> np.ndarray:
    iso = dates.isocalendar()
    week_id = pd.Series(
        iso["year"].astype(str) + "-" + iso["week"].astype(str), index=dates
    )
    return (~week_id.duplicated(keep="first")).to_numpy()


def ic_stats(factor: pd.DataFrame, fwd: pd.DataFrame) -> dict:
    series = compute_ic_series(factor, fwd)
    s = series.dropna()
    if s.empty:
        return {"ic": np.nan, "ir": np.nan, "n": 0, "pos_ratio": np.nan}
    ic = float(s.mean())
    std = float(s.std(ddof=1)) if len(s) > 1 else np.nan
    ir = float(ic / std) if std and std > 0 else np.nan
    return {
        "ic": ic,
        "ir": ir,
        "n": int(len(s)),
        "pos_ratio": float((s > 0).mean()),
    }


def mean_corr(a: pd.DataFrame, b: pd.DataFrame) -> tuple[float, float]:
    """Return (mean |Spearman|, mean signed Spearman) across dates."""
    vals = []
    for dt in a.index.intersection(b.index):
        x = a.loc[dt]
        y = b.loc[dt]
        m = x.notna() & y.notna()
        if int(m.sum()) < 20:
            continue
        # Rank then Pearson — avoids scipy dependency for Spearman.
        vals.append(float(x[m].rank().corr(y[m].rank())))
    if not vals:
        return float("nan"), float("nan")
    arr = np.asarray(vals, dtype=float)
    return float(np.nanmean(np.abs(arr))), float(np.nanmean(arr))


def confirm_score(
    smooth: dict[str, pd.DataFrame],
    ids: list[str],
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    ref = next(iter(smooth.values()))
    num = pd.DataFrame(0.0, index=ref.index, columns=ref.columns)
    den = pd.DataFrame(0.0, index=ref.index, columns=ref.columns)
    for aid in ids:
        sm = smooth.get(aid)
        if sm is None:
            continue
        w = 1.0 if weights is None else float(weights.get(aid, 1.0))
        valid = sm.notna()
        num = num.add(sm.fillna(0.0) * w, fill_value=0.0)
        den = den.add(valid.astype(float) * w, fill_value=0.0)
    return num.divide(den.replace(0.0, np.nan))


def pass_rate_on_topn(
    sort_score: pd.DataFrame,
    confirm: pd.DataFrame,
    is_reb: np.ndarray,
    top_n: int = 6,
    q: float = 0.40,
) -> dict:
    """On rebalance days: fraction of TopN-by-S that pass confirm>=q."""
    thr = confirm.quantile(q, axis=1)
    ok = confirm.ge(thr, axis=0)
    ranks = sort_score.rank(axis=1, ascending=False, method="first")
    rates = []
    blocked_fwd = []
    kept_fwd = []
    close = sort_score  # placeholder overwritten by caller via closure? no — fix below
    return {
        "ok": ok,
        "ranks": ranks,
        "thr": thr,
        "rates": rates,
        "blocked_fwd": blocked_fwd,
        "kept_fwd": kept_fwd,
    }


def analyze_filter_cost(
    sort_score: pd.DataFrame,
    confirm: pd.DataFrame,
    fwd5: pd.DataFrame,
    is_reb: np.ndarray,
    top_n: int = 6,
    q: float = 0.40,
) -> dict:
    thr = confirm.quantile(q, axis=1)
    ok = confirm.ge(thr, axis=0).fillna(False)
    ranks = sort_score.rank(axis=1, ascending=False, method="first")
    pass_rates = []
    blocked_rets = []
    passed_rets = []
    for i, dt in enumerate(sort_score.index):
        if not is_reb[i]:
            continue
        top = ranks.loc[dt]
        top_names = top[top <= top_n].index
        if len(top_names) == 0:
            continue
        passed = [c for c in top_names if bool(ok.loc[dt, c])]
        blocked = [c for c in top_names if c not in passed]
        pass_rates.append(len(passed) / len(top_names))
        for c in blocked:
            r = fwd5.loc[dt, c] if c in fwd5.columns else np.nan
            if pd.notna(r):
                blocked_rets.append(float(r))
        for c in passed:
            r = fwd5.loc[dt, c] if c in fwd5.columns else np.nan
            if pd.notna(r):
                passed_rets.append(float(r))
    return {
        "topn_pass_rate": float(np.mean(pass_rates)) if pass_rates else np.nan,
        "blocked_fwd5_mean": float(np.mean(blocked_rets)) if blocked_rets else np.nan,
        "passed_fwd5_mean": float(np.mean(passed_rets)) if passed_rets else np.nan,
        "n_blocked": len(blocked_rets),
        "n_passed": len(passed_rets),
        "block_minus_pass_fwd5": (
            float(np.mean(blocked_rets) - np.mean(passed_rets))
            if blocked_rets and passed_rets
            else np.nan
        ),
    }


def long_short_spread(
    score: pd.DataFrame, fwd: pd.DataFrame, is_reb: np.ndarray, n: int = 6
) -> float:
    """Equal-weight TopN minus BottomN forward return on rebalance days."""
    spreads = []
    ranks = score.rank(axis=1, ascending=False, method="first")
    for i, dt in enumerate(score.index):
        if not is_reb[i]:
            continue
        r = ranks.loc[dt]
        top = r[r <= n].index
        bot = r[r > (r.notna().sum() - n)].index
        if len(top) == 0 or len(bot) == 0:
            continue
        ft = fwd.loc[dt, top].mean()
        fb = fwd.loc[dt, bot].mean()
        if pd.notna(ft) and pd.notna(fb):
            spreads.append(float(ft - fb))
    return float(np.mean(spreads)) if spreads else float("nan")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = load_panel(ARTIFACTS)
    close = panel["close"]
    print(f"panel: {close.shape[0]} days x {close.shape[1]} names")

    registry = Registry()
    raw: dict[str, pd.DataFrame] = {}
    smooth: dict[str, pd.DataFrame] = {}
    for aid in ALL_IDS:
        try:
            f = registry.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {aid}: {exc}")
            continue
        raw[aid] = f
        smooth[aid] = smooth_cs(f)
        print(f"ok {aid}")

    # Forward returns (no lookahead in factor; fwd is evaluation only).
    fwd5 = close.shift(-5) / close - 1.0
    fwd10 = close.shift(-10) / close - 1.0
    is_reb = weekly_mask(close.index)

    # 1) IC table
    ic_rows = []
    for aid, sm in smooth.items():
        s5 = ic_stats(sm, fwd5)
        s10 = ic_stats(sm, fwd10)
        abs_corr, signed_corr = mean_corr(sm, smooth[MOM_ID])
        ic_rows.append(
            {
                "id": aid,
                "role": (
                    "mom"
                    if aid == MOM_ID
                    else ("C_confirm" if aid in C_CONFIRM else ("E_confirm" if aid in E_CONFIRM else "extra"))
                ),
                "ic5": s5["ic"],
                "ir5": s5["ir"],
                "pos5": s5["pos_ratio"],
                "ic10": s10["ic"],
                "ir10": s10["ir"],
                "corr164_signed": signed_corr,
                "corr164_abs": abs_corr,
            }
        )
    ic_df = pd.DataFrame(ic_rows).sort_values("ir5", ascending=False)
    ic_df.to_csv(OUT_DIR / "ic_table.csv", index=False)
    print("\n=== IC / corr vs 164 ===")
    print(ic_df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    # 2) Filter cost: C vs E hard confirm on Top6
    c_conf = confirm_score(
        smooth,
        C_CONFIRM,
        {"gtja191_114": 0.176, "gtja191_171": 0.155, "gtja191_054": 0.152},
    )
    e_conf = confirm_score(smooth, E_CONFIRM)
    mom = smooth[MOM_ID]
    cost_c = analyze_filter_cost(mom, c_conf, fwd5, is_reb)
    cost_e = analyze_filter_cost(mom, e_conf, fwd5, is_reb)
    print("\n=== Top6-by-164 confirm pass / opportunity cost (fwd5) ===")
    print("C:", json.dumps(cost_c, indent=2))
    print("E:", json.dumps(cost_e, indent=2))

    # Per-leg pass rate on Top6
    leg_rows = []
    for aid in C_CONFIRM + E_CONFIRM + EXTRA:
        if aid not in smooth:
            continue
        cost = analyze_filter_cost(mom, smooth[aid], fwd5, is_reb)
        leg_rows.append({"id": aid, **cost})
    leg_df = pd.DataFrame(leg_rows).sort_values("block_minus_pass_fwd5")
    leg_df.to_csv(OUT_DIR / "leg_filter_cost.csv", index=False)
    print("\n=== Per-leg filter cost (negative block-pass = filter hurts) ===")
    print(leg_df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    # 3) Counterfactual soft designs on rebalance days (signal-only, not full engine)
    #    a) pure 164 Top6
    #    b) C hard filter
    #    c) E hard filter
    #    d) soft: S' = 0.7*S + 0.3*C  then Top6 (no hard cut)
    #    e) hybrid hard: C confirms + best E leg by IR
    #    f) E with q=0.20 (looser)
    #    g) only keep E legs with positive IC5 and corr164>=0
    designs = {}
    designs["pure_164"] = mom
    designs["C_hard_score"] = mom.where(
        mom.ge(0) | True
    )  # ranking still mom; filter applied below

    def topn_mean_fwd(score, conf_ok, fwd, is_reb, n=6):
        ranks = score.rank(axis=1, ascending=False, method="first")
        rets = []
        for i, dt in enumerate(score.index):
            if not is_reb[i]:
                continue
            r = ranks.loc[dt]
            # candidates: rank<=n after optional mask
            if conf_ok is not None:
                # re-rank among those who pass, take top n by original sort
                eligible = conf_ok.loc[dt].fillna(False)
                pool = r[eligible].sort_values()
            else:
                pool = r.dropna().sort_values()
            picks = pool.index[:n]
            if len(picks) == 0:
                continue
            fr = fwd.loc[dt, picks].mean()
            if pd.notna(fr):
                rets.append(float(fr))
        return float(np.mean(rets)) if rets else float("nan"), len(rets)

    def hard_ok(conf, q):
        thr = conf.quantile(q, axis=1)
        return conf.ge(thr, axis=0).fillna(False)

    rows = []
    for name, conf, q, soft in [
        ("pure_164_top6", None, None, False),
        ("C_hard_q40", c_conf, 0.40, False),
        ("E_hard_q40", e_conf, 0.40, False),
        ("E_hard_q20", e_conf, 0.20, False),
        ("E_hard_q60", e_conf, 0.60, False),
    ]:
        ok = None if conf is None else hard_ok(conf, q)
        mean_r, n = topn_mean_fwd(mom, ok, fwd5, is_reb)
        rows.append({"design": name, "mean_fwd5_top6": mean_r, "n_reb": n})

    # Soft blends: combine sort with confirm without hard kill.
    for blend_name, conf, w_c in [
        ("soft_C_70_30", c_conf, 0.30),
        ("soft_E_70_30", e_conf, 0.30),
        ("soft_E_50_50", e_conf, 0.50),
        ("soft_C_50_50", c_conf, 0.50),
    ]:
        # Both already percentile-smoothed; blend then Top6.
        score = (1.0 - w_c) * mom + w_c * conf
        mean_r, n = topn_mean_fwd(score, None, fwd5, is_reb)
        rows.append({"design": blend_name, "mean_fwd5_top6": mean_r, "n_reb": n})

    # Hybrid: keep C hard filter, add only positively complementary E legs into confirm.
    good_e = [
        aid
        for aid in E_CONFIRM
        if aid in smooth
        and float(ic_df.loc[ic_df["id"] == aid, "ic5"].iloc[0]) > 0
        and float(ic_df.loc[ic_df["id"] == aid, "corr164_signed"].iloc[0]) > -0.05
    ]
    hybrid_ids = C_CONFIRM + good_e
    if hybrid_ids:
        h_conf = confirm_score(smooth, hybrid_ids)
        ok = hard_ok(h_conf, 0.40)
        mean_r, n = topn_mean_fwd(mom, ok, fwd5, is_reb)
        rows.append(
            {
                "design": f"hybrid_C_plus_goodE({'+'.join(good_e) or 'none'})",
                "mean_fwd5_top6": mean_r,
                "n_reb": n,
            }
        )

    # Momentum-aligned only: drop anti-momentum quality legs
    aligned = [
        aid
        for aid in E_CONFIRM
        if aid in smooth
        and float(ic_df.loc[ic_df["id"] == aid, "corr164_signed"].iloc[0]) >= 0
    ]
    if aligned:
        a_conf = confirm_score(smooth, aligned)
        ok = hard_ok(a_conf, 0.40)
        mean_r, n = topn_mean_fwd(mom, ok, fwd5, is_reb)
        rows.append(
            {
                "design": f"E_aligned_only({'+'.join(aligned)})",
                "mean_fwd5_top6": mean_r,
                "n_reb": n,
            }
        )

    # risk_mom as alternate primary
    if "custom_risk_mom" in smooth:
        mean_r, n = topn_mean_fwd(smooth["custom_risk_mom"], None, fwd5, is_reb)
        rows.append({"design": "primary_risk_mom_top6", "mean_fwd5_top6": mean_r, "n_reb": n})
        # 164 + risk_mom average
        combo = 0.5 * mom + 0.5 * smooth["custom_risk_mom"]
        mean_r, n = topn_mean_fwd(combo, hard_ok(c_conf, 0.40), fwd5, is_reb)
        rows.append({"design": "combo164_riskmom_Cq40", "mean_fwd5_top6": mean_r, "n_reb": n})

    des_df = pd.DataFrame(rows).sort_values("mean_fwd5_top6", ascending=False)
    des_df.to_csv(OUT_DIR / "design_counterfactual.csv", index=False)
    print("\n=== Counterfactual Top6 mean fwd5 (signal-only) ===")
    print(des_df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    # 4) Position-level C vs E divergence from saved artifacts
    pos_c = pd.read_csv(
        _AGENT_ROOT / "runs/20260725_094658_01_531d4f/artifacts/positions.csv",
        index_col=0,
        parse_dates=True,
    )
    pos_e = pd.read_csv(
        _AGENT_ROOT / "runs/20260725_175000_01_quality_e/artifacts/positions.csv",
        index_col=0,
        parse_dates=True,
    )
    common_idx = pos_c.index.intersection(pos_e.index)
    common_cols = pos_c.columns.intersection(pos_e.columns)
    pc = pos_c.loc[common_idx, common_cols].fillna(0.0)
    pe = pos_e.loc[common_idx, common_cols].fillna(0.0)
    # Daily contribution of names held by C not E
    only_c = (pc > 0) & (pe <= 0)
    only_e = (pe > 0) & (pc <= 0)
    # Use next-day close-to-close as attribution proxy
    ret1 = close.pct_change().reindex(index=common_idx, columns=common_cols)
    # Weight-normalized active book return of exclusive holdings
    def book_ret(mask, pos, r):
        w = pos.where(mask, 0.0)
        # scale to the weight actually allocated that day
        gross = w.sum(axis=1).replace(0, np.nan)
        # contribution ≈ sum w_i * r_i  (weights already portfolio weights)
        return (w * r).sum(axis=1)

    # Shift returns to align position_t with return_{t->t+1}
    r_next = ret1.shift(-1)
    contrib_c_only = book_ret(only_c, pc, r_next)
    contrib_e_only = book_ret(only_e, pe, r_next)
    summary = {
        "mean_names_only_C": float(only_c.sum(axis=1).mean()),
        "mean_names_only_E": float(only_e.sum(axis=1).mean()),
        "cum_contrib_only_C": float(contrib_c_only.fillna(0).sum()),
        "cum_contrib_only_E": float(contrib_e_only.fillna(0).sum()),
        "delta_exclusive_contrib_C_minus_E": float(
            contrib_c_only.fillna(0).sum() - contrib_e_only.fillna(0).sum()
        ),
    }
    (OUT_DIR / "position_divergence.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print("\n=== Position divergence attribution ===")
    print(json.dumps(summary, indent=2))

    # Persist a short markdown verdict
    best = des_df.iloc[0]
    verdict = {
        "cost_c": cost_c,
        "cost_e": cost_e,
        "best_design": best.to_dict(),
        "good_e_legs": good_e,
        "aligned_e_legs": aligned,
        "position_divergence": summary,
    }
    (OUT_DIR / "verdict.json").write_text(
        json.dumps(verdict, indent=2, default=float), encoding="utf-8"
    )
    print(f"\nWrote outputs under {OUT_DIR}")


if __name__ == "__main__":
    main()
