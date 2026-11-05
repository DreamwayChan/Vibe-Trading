"""Confirm-leg robustness across momentum legs (GTJA191 alive set).

Picking the best ``(momentum leg, confirm leg)`` cell by raw excess on a 9x37
grid is exactly the overfit trap: the winner's neighbours can flip sign. A
confirm leg should instead be judged by how it behaves *when the sort leg
changes*, so this script fixes six candidate confirm sets and runs each one
against **every** momentum leg through the market engine, then ranks the sets
by mean / median / worst-case excess.

Run from ``agent/``:
    python scripts/ablate_gtja_confirm.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

import scripts.research_gtja_trendstock_confirm_search as G  # noqa: E402
from scripts.research_qlib158_trendstock_confirm_search import _num, _pct  # noqa: E402

TOP_SETS = 6
FORCE = [
    ("gtja191_002", "gtja191_168"),
    ("gtja191_111", "gtja191_002"),
    ("gtja191_111", "gtja191_178"),
    ("gtja191_111",),
]


def main() -> None:
    rows = G._survival_rows()
    alive = G._ids_by_category(rows, "alive")
    irs = G._irs(rows, alive)
    data_map = G._load_ohlcv_src(G.SRC_RUN)
    panel = G._build_panel(data_map)
    close = panel["close"]
    fwd = G._forward_returns(close)
    scores, _ic = G._compute_all(panel, alive + [G.REFERENCE_COMBO[0]], fwd)
    mom_legs = [a for a in alive if a in scores] + [G.REFERENCE_COMBO[0]]

    fast = pd.read_csv(G.OUT_ROOT / "gtja_fast_screen.csv")
    rows_mean: list[tuple[str, float, int]] = []
    for key, grp in fast[fast.n_confirm >= 1].groupby("confirm"):
        rows_mean.append((key, float(grp.excess_return.mean()), len(grp)))
    rows_mean.sort(key=lambda t: -t[1])
    picked: list[tuple[str, ...]] = [c for c in FORCE]
    for key, _mean, _n in rows_mean:
        tup = tuple(key.split("+"))
        ids = tuple(G.PREFIX + t for t in tup)
        if ids in picked or len(ids) > 2:
            continue
        picked.append(ids)
        if len(picked) >= TOP_SETS:
            break
    print(f"[candidates] {[G._lab(c) for c in picked]}")

    out_rows: list[dict] = []
    for c in picked:
        for mom in mom_legs:
            if mom in c:
                continue
            run_dir = G.OUT_ROOT / f"xabl_{mom.replace(G.PREFIX, '')}__{G._lab(c)}"
            res = G._run(mom, list(c), scores, irs, data_map, run_dir, 0)
            m = res["metrics"]
            out_rows.append(
                {
                    "confirm": G._lab(c),
                    "confirm_ids": list(c),
                    "mom": mom.replace(G.PREFIX, ""),
                    "mom_id": mom,
                    "excess_return": float(m.get("excess_return", 0.0)),
                    "total_return": float(m.get("total_return", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "run_dir": res["run_dir"],
                }
            )
        sub = pd.DataFrame([r for r in out_rows if r["confirm"] == G._lab(c)])
        print(
            f"  {G._lab(c)}: mean={sub.excess_return.mean():+.4f} "
            f"median={sub.excess_return.median():+.4f} min={sub.excess_return.min():+.4f} "
            f"pos={int((sub.excess_return > 0).sum())}/{len(sub)}"
        )

    df = pd.DataFrame(out_rows)
    df.to_csv(G.OUT_ROOT / "gtja_confirm_robustness.csv", index=False, encoding="utf-8")
    agg = (
        df.groupby("confirm")
        .agg(
            mean_excess=("excess_return", "mean"),
            median_excess=("excess_return", "median"),
            min_excess=("excess_return", "min"),
            max_excess=("excess_return", "max"),
            mean_sharpe=("sharpe", "mean"),
            n=("excess_return", "size"),
        )
        .sort_values("mean_excess", ascending=False)
    )
    agg["pos_ratio"] = df.groupby("confirm").excess_return.apply(lambda s: float((s > 0).mean()))
    agg.to_csv(G.OUT_ROOT / "gtja_confirm_robustness_agg.csv", encoding="utf-8")
    print(agg.round(4).to_string())

    md = ["", "### 4. 确认腿稳健性（每个确认腿跑满 9 条动量腿，市场引擎）", "",
          "| 确认腿 | 平均 excess | 中位 | 最差 | 最好 | 正超额腿数 | 平均 sharpe |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name, r in agg.iterrows():
        md.append(
            f"| `{name}` | {_pct(r.mean_excess)} | {_pct(r.median_excess)} | {_pct(r.min_excess)} | "
            f"{_pct(r.max_excess)} | {int(r.pos_ratio * r.n)}/{int(r.n)} | {_num(r.mean_sharpe)} |"
        )
    md.append("")
    path = G.OUT_ROOT / "robustness_confirm.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"[done] {path}")


if __name__ == "__main__":
    raise SystemExit(main())
