"""Marginal value of each GTJA confirm leg: Δexcess vs the same momentum leg.

Comparing raw excess across momentum legs mixes two effects (how good the sort
leg is vs how much the confirm leg adds). This script runs every momentum leg
*without* a confirm leg through the engine and reports Δexcess = confirm - pure
per cell, which is the honest measure of "最优确认脚".

Run from ``agent/`` (after ``ablate_gtja_confirm.py``):
    python scripts/delta_gtja_confirm.py
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

    base: dict[str, float] = {}
    for mom in mom_legs:
        res = G._run(mom, [], scores, irs, data_map, G.OUT_ROOT / f"pure_{mom.replace(G.PREFIX, '')}", 0)
        base[mom] = float(res["metrics"].get("excess_return", 0.0))
        print(f"[pure] {mom.replace(G.PREFIX, '')} excess={base[mom]:+.4f}")
    pd.Series(base).to_csv(G.OUT_ROOT / "gtja_pure_baseline.csv", header=["excess_return"], encoding="utf-8")

    df = pd.read_csv(G.OUT_ROOT / "gtja_confirm_robustness.csv")
    df["pure_excess"] = df.mom_id.map(base)
    df["delta"] = df.excess_return - df.pure_excess
    df.to_csv(G.OUT_ROOT / "gtja_confirm_robustness.csv", index=False, encoding="utf-8")

    agg = (
        df.groupby("confirm")
        .agg(
            mean_delta=("delta", "mean"),
            median_delta=("delta", "median"),
            min_delta=("delta", "min"),
            max_delta=("delta", "max"),
            mean_abs_excess=("excess_return", "mean"),
            n=("delta", "size"),
        )
        .sort_values("mean_delta", ascending=False)
    )
    agg["pos_ratio"] = df.groupby("confirm").delta.apply(lambda s: float((s > 0).mean()))
    agg.to_csv(G.OUT_ROOT / "gtja_confirm_delta_agg.csv", encoding="utf-8")
    print(agg.round(4).to_string())

    md = ["", "### 5. 确认腿的边际价值（Δexcess = 有确认腿 − 同动量腿无确认腿）", "",
          "| 确认腿 | 平均 Δexcess | 中位 | 最差 | 最好 | 正增益腿数 | 平均 excess |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name, r in agg.iterrows():
        md.append(
            f"| `{name}` | {_pct(r.mean_delta)} | {_pct(r.median_delta)} | {_pct(r.min_delta)} | "
            f"{_pct(r.max_delta)} | {int(r.pos_ratio * r.n)}/{int(r.n)} | {_pct(r.mean_abs_excess)} |"
        )
    md.append("")
    md += ["| 动量腿 | 无确认腿 excess |", "|---|---:|"]
    for mom, v in sorted(base.items(), key=lambda kv: -kv[1]):
        md.append(f"| `{mom.replace(G.PREFIX, '')}` | {_pct(v)} |")
    md.append("")
    path = G.OUT_ROOT / "robustness_delta.md"
    path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"[done] {path}")


if __name__ == "__main__":
    raise SystemExit(main())
