"""Robustness checks for the GTJA191 winner on 趋势股 BK1715.

1. split-sample re-runs (2024 / 2025 / 2026 YTD) through the market engine;
2. confirm-leg ladder: pure momentum -> single factor -> pair, i.e. the actual
   marginal contribution of the confirmation leg;
3. cross-zoo swap: same GTJA momentum leg with the qlib158 winner's confirm
   legs (``cord30+ksft``), to separate "factor source" from "portfolio process";
4. return attribution / concentration on the published run.

Run from ``agent/``:
    python scripts/analyze_gtja_winner.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

import scripts.research_gtja_trendstock_confirm_search as G  # noqa: E402
from scripts.research_qlib158_trendstock_confirm_search import (  # noqa: E402
    _num,
    _pct,
)

OUT_ROOT = G.OUT_ROOT
SPLITS = [
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
    ("2026-01-01", "2026-11-03"),
]


def _slice(data_map: dict[str, pd.DataFrame], start: str, end: str) -> dict[str, pd.DataFrame]:
    out = {}
    for code, df in data_map.items():
        sub = df.loc[(df.index >= start) & (df.index <= end)]
        if len(sub) >= 120:
            out[code] = sub
    return out


def _attribution(run_dir: Path, data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True, header=0)
    close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).reindex(pos.index)
    rets = close.pct_change().fillna(0.0)
    contrib = pos.shift(1).fillna(0.0) * rets
    total = contrib.sum(axis=0).sort_values(ascending=False)
    gross = float(total.sum())
    return pd.DataFrame(
        {
            "code": total.index,
            "contrib": total.values,
            "share": total.values / gross if gross else np.zeros(len(total)),
            "days_held": (pos[total.index] > 0).sum().values,
        }
    )


def _p(metrics: dict, key: str = "p_value_sharpe") -> str:
    return _num(metrics.get(key))


def main() -> None:
    # ---- reload winner config from the published run
    runs = sorted((_AGENT_ROOT / "runs").glob("*_05_F0_trendstock_gtja_*"))
    if not runs:
        raise SystemExit("no published GTJA run")
    winner_run = runs[-1]
    ctx = json.loads((winner_run / "req.json").read_text(encoding="utf-8"))["context"]
    mom_id = ctx["mom_id"]
    confirm_ids = list(ctx["confirm_ids"])
    print(f"[winner] {winner_run.name} mom={mom_id} confirm={confirm_ids}")

    rows = G._survival_rows()
    alive = G._ids_by_category(rows, "alive")
    irs = G._irs(rows, alive)
    data_map = G._load_ohlcv_src(G.SRC_RUN)
    panel = G._build_panel(data_map)
    close = panel["close"]
    fwd = G._forward_returns(close)
    scores, ic = G._compute_all(panel, alive + ["qlib158_cord30", "qlib158_ksft"], fwd)

    lines: list[str] = ["", "## 稳健性检查（GTJA191 存活因子）", ""]

    # ---- 1. split sample
    lines += [
        "### 1. 分段样本（同参数、独立区间重跑市场引擎）",
        "",
        "| 区间 | total | benchmark | excess | sharpe | max_dd | MC p_sharpe |",
        "|------|------:|----------:|-------:|-------:|-------:|------------:|",
    ]
    for start, end in SPLITS:
        sub = _slice(data_map, start, end)
        old = (G.START_DATE, G.END_DATE)
        G.START_DATE, G.END_DATE = start, end
        try:
            res = G._run(
                mom_id, confirm_ids, scores, irs, sub,
                OUT_ROOT / f"split_{start}_{end}", 500,
            )
            m = res["metrics"]
            p = ((res.get("validation") or {}).get("monte_carlo") or {}).get("p_value_sharpe")
            lines.append(
                f"| {start} ~ {end} | {_pct(m.get('total_return'))} | {_pct(m.get('benchmark_return'))} | "
                f"{_pct(m.get('excess_return'))} | {_num(m.get('sharpe'))} | {_pct(m.get('max_drawdown'))} | {_num(p)} |"
            )
            print(f"[split] {start}~{end} excess={float(m.get('excess_return')):+.4f} p={p}")
        finally:
            G.START_DATE, G.END_DATE = old

    # ---- 2. confirm-leg ladder (full period)
    ladder: list[tuple[str, list[str]]] = [
        ("pure（无确认腿）", []),
        (f"只 {confirm_ids[0].replace(G.PREFIX, '')}", [confirm_ids[0]]),
        (f"只 {confirm_ids[1].replace(G.PREFIX, '')}", [confirm_ids[1]]),
        (f"{confirm_ids[0].replace(G.PREFIX, '')}+{confirm_ids[1].replace(G.PREFIX, '')}", list(confirm_ids)),
        ("qlib158 cord30+ksft（跨库对照）", ["qlib158_cord30", "qlib158_ksft"]),
    ]
    ladder_md = ["", "### 2. 确认腿阶梯（同一动量腿，只改确认腿）", "",
                 "| 确认腿 | total | excess | Δexcess vs pure | sharpe | max_dd |", "|---|---:|---:|---:|---:|---:|"]
    base_excess: float | None = None
    for name, cids in ladder:
        res = G._run(
            mom_id, cids, scores, irs, data_map,
            OUT_ROOT / f"ladder_{name.replace(' ', '_').replace('（', '').replace('）', '')[:18]}", 0,
        )
        m = res["metrics"]
        ex = float(m.get("excess_return", 0.0))
        if base_excess is None:
            base_excess = ex
        ladder_md.append(
            f"| {name} | {_pct(m.get('total_return'))} | {_pct(ex)} | {_pct(ex - base_excess)} | "
            f"{_num(m.get('sharpe'))} | {_pct(m.get('max_drawdown'))} |"
        )
        print(f"[ladder] {name}: excess={ex:+.4f}")

    # ---- 3. attribution
    attr = _attribution(winner_run, data_map)
    names = G._name_map()
    top5 = attr.head(5)
    attr_md = ["", "### 3. 收益归因（前 5 名）", "",
               "| 代码 | 名称 | 累计贡献 | 占毛收益 | 持仓天数 |", "|---|---|---:|---:|---:|"]
    for row in top5.itertuples(index=False):
        attr_md.append(
            f"| {row.code} | {names.get(row.code, '')} | {_pct(row.contrib)} | {_pct(row.share)} | {int(row.days_held)} |"
        )
    attr_md += [
        "",
        f"- 前 5 名合计占毛收益 **{_pct(top5['share'].sum())}**；正贡献 {int((attr['contrib'] > 0).sum())} / {len(attr)}；"
        f"负贡献合计 {_pct(attr[attr['contrib'] < 0]['contrib'].sum())}",
        "",
    ]

    out = OUT_ROOT / "robustness_gtja.md"
    out.write_text("\n".join(lines + ladder_md + attr_md) + "\n", encoding="utf-8")
    print(f"[done] {out}")


if __name__ == "__main__":
    raise SystemExit(main())
