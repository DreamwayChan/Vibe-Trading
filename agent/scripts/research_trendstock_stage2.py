"""Stage 2 on 趋势股 BK1715: engine ranking + momentum-leg ablation.

Stage 1 ranked combos with the in-memory fast path, and on this pool the fast
path inverted the ranking (a +4.2% fast excess turned into -10% in the market
engine). Stage 2 therefore:

1. re-ranks a shortlist of confirm combos with the real market engine (mc=0);
2. ablates the momentum sort leg (std30 vs the highest-IR BK1715 factors);
3. Monte-Carlo verifies the top rows and publishes the winner as a run.

Run from ``agent/``:
    python scripts/research_trendstock_stage2.py --top-k 6
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine  # noqa: E402
from scripts.research_copper_confirm_search import (  # noqa: E402
    _build_weights,
    _confirm_ok,
    _fast_metrics,
    _smooth_cs,
)
from scripts.research_qlib158_trendstock_confirm_search import (  # noqa: E402
    BANNED_CONFIRM,
    END_DATE,
    INITIAL_CASH,
    NAIVE_CONFIRM,
    PANEL,
    REFERENCE_COMBOS,
    SRC_RUN,
    START_DATE,
    UNIVERSE_DESC,
    _build_panel,
    _combo_label,
    _forward_returns,
    _holdings_md,
    _load_irs,
    _load_ohlcv_src,
    _mc_p,
    _name_map,
    _num,
    _pct,
    _pool_ic,
)
from src.factors.registry import Registry  # noqa: E402

OUT_ROOT = _AGENT_ROOT / "runs" / "_research_qlib158_trendstock_stage2"
PROP_OUT = _AGENT_ROOT / "prop_qlib158_趋势股_最优.md"
SUMMARY_OUT = OUT_ROOT / "stage2_summary.md"

BASE_MOM = "qlib158_std30"
MOM_LEGS = ["qlib158_std30", "qlib158_vma60", "qlib158_klen", "qlib158_vsumn60", "qlib158_vma30"]
STAGE1_TOP = [
    ("qlib158_cord30", "qlib158_ksft"),
    ("qlib158_cord30", "qlib158_kmid2"),
    ("qlib158_cord30", "qlib158_qtld30"),
    ("qlib158_cord30", "qlib158_ksft2"),
    ("qlib158_klen", "qlib158_qtld30"),
    ("qlib158_kmid", "qlib158_max60"),
    ("qlib158_ksft2", "qlib158_max60"),
    ("qlib158_cord20", "qlib158_std5"),
    ("qlib158_cord20", "qlib158_vsumn30"),
    ("qlib158_cord30", "qlib158_klen"),
]


class _Engine:
    """SignalEngine adapter: configurable momentum leg, precomputed scores."""

    def __init__(self, mom_id, confirm_ids, scores, irs, *, veto_max=False):
        self.mom_id = mom_id
        self.confirm_ids = list(confirm_ids)
        self.alpha_ids = [mom_id] + self.confirm_ids
        if veto_max:
            self.alpha_ids.append("custom_max_inv")
        self.scores = scores
        self.irs = irs
        self.veto_max = veto_max
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).sort_index()
        mom = self.scores[self.mom_id].reindex(index=close.index, columns=close.columns)
        ok = _confirm_ok(tuple(self.confirm_ids), self.scores, self.irs, close)
        if self.veto_max and "custom_max_inv" in self.scores:
            mx = self.scores["custom_max_inv"].reindex(index=close.index, columns=close.columns)
            thr = mx.quantile(0.20, axis=1)
            ok = ok & mx.ge(thr, axis=0).fillna(True)
        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _run(
    mom_id: str,
    confirm_ids: list[str],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame],
    run_dir: Path,
    mc_sims: int,
    *,
    veto_max: bool = False,
) -> dict:
    label_pre = f"{mom_id.replace('qlib158_', '')}__{_combo_label(tuple(confirm_ids))}" + (
        "_F1" if veto_max else ""
    )
    metrics_path = run_dir / "artifacts" / "metrics.csv"
    val_path0 = run_dir / "artifacts" / "validation.json"
    reusable = metrics_path.exists() and (
        (mc_sims <= 0) or val_path0.exists()
    )
    if reusable:
        metrics = pd.read_csv(metrics_path).iloc[0].to_dict()
        validation = json.loads(val_path0.read_text(encoding="utf-8")) if val_path0.exists() else {}
        print(f"  [reuse] {label_pre}")
        return {
            "label": label_pre,
            "mom_id": mom_id,
            "confirm_ids": list(confirm_ids),
            "veto_max": veto_max,
            "metrics": metrics,
            "validation": validation,
            "is_rebalance_days": int(metrics.get("rebalance_count") or 0),
            "run_dir": str(run_dir),
        }
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{run_dir.name}_r{int(time.time()) % 100000}")
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    if veto_max and "custom_max_inv" not in scores:
        raw = Registry().compute("custom_max_inv", _build_panel(data_map))
        if raw is None or raw.empty:
            raise SystemExit("custom_max_inv empty")
        scores["custom_max_inv"] = _smooth_cs(
            raw.reindex(index=raw.index, columns=raw.columns)
        )

    leg = mom_id.replace("qlib158_", "")
    label = f"{leg}__{_combo_label(tuple(confirm_ids))}" + ("_F1" if veto_max else "")
    alphas = [mom_id] + list(confirm_ids) + (["custom_max_inv"] if veto_max else [])
    cfg = {
        "codes": sorted(data_map),
        "source": "tushare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": INITIAL_CASH,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": alphas,
        "parameters": {
            "strategy": f"qlib158_trendstock_{leg}__{_combo_label(tuple(confirm_ids))}"
            + ("_F1" if veto_max else ""),
            "panel": PANEL,
            "mom_id": mom_id,
            "confirm_ids": list(confirm_ids),
            "zoo": "qlib158",
        },
        "_run_card_effective_sources": ["local_artifacts"],
    }
    if mc_sims >= 1:
        cfg["validation"] = {"monte_carlo": {"n_simulations": mc_sims}}
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sig = _Engine(mom_id, confirm_ids, scores, irs, veto_max=veto_max)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    engine.run_backtest(
        cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=calc_bars_per_year("1D", "tushare")
    )
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "label": label,
        "mom_id": mom_id,
        "confirm_ids": list(confirm_ids),
        "veto_max": veto_max,
        "metrics": metrics,
        "validation": validation,
        "is_rebalance_days": sig.last_is_rebalance_days,
        "run_dir": str(run_dir),
    }


def _publish(winner: dict, name_map: dict[str, str]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mom = winner["mom_id"].replace("qlib158_", "")
    legs = "_".join(a.replace("qlib158_", "") for a in winner["confirm_ids"]) or "pure"
    run_id = f"{stamp}_05_F0_trendstock_{mom}__{legs}"
    dest = _AGENT_ROOT / "runs" / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(Path(winner["run_dir"]), dest)
    title = f"趋势股BK1715 F0' {mom}+{legs} — Qlib158最优因子组合"
    prompt = (
        f"市场扫描第2板块 趋势股 BK1715 成分股回测（80只，2024-01-01~2026-11-03）："
        f"动量 {mom} + 硬确认 {legs}，参考金属铜/存储芯片 F0' 过程，两段式搜索（快筛+引擎排名+动量腿消融）"
        f"选出 excess_return 最优组合。"
    )
    (dest / "req.json").write_text(
        json.dumps(
            {
                "title": title,
                "prompt": prompt,
                "context": {
                    "panel": PANEL,
                    "bk": "BK1715",
                    "market_pick_rank": 2,
                    "mom_id": winner["mom_id"],
                    "confirm_ids": winner["confirm_ids"],
                    "universe_run": SRC_RUN.name,
                    "search_run": Path(winner["run_dir"]).name,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (dest / "state.json").write_text(
        json.dumps({"status": "success", "source": "research_trendstock_stage2"}, ensure_ascii=False),
        encoding="utf-8",
    )
    m = winner["metrics"]
    hold_md, hold_stats = _holdings_md(dest, name_map)
    body = f"""# {title}

**研究/回测，非实盘建议。**

## 设定

- 标的池：{UNIVERSE_DESC}
- 区间：{START_DATE} ~ {END_DATE}；source=lake（本地日线）；周频首个交易日调仓
- 动量腿：`{winner["mom_id"]}`；确认腿：`{", ".join(a.replace("qlib158_", "") for a in winner["confirm_ids"])}`
- 过程：N=6 等权 / 保留带 Top9 / 换手帽 20% / 最小持有 5 日 / 峰值回撤 15% 止损 / 止盈 35% / MA20 择时

## 指标

| 指标 | 值 |
|------|----|
| total_return | {_pct(m.get("total_return"))} |
| benchmark_return | {_pct(m.get("benchmark_return"))} |
| excess_return | {_pct(m.get("excess_return"))} |
| sharpe | {_num(m.get("sharpe"))} |
| max_drawdown | {_pct(m.get("max_drawdown"))} |
| trade_count | {int(float(m.get("trade_count") or 0))} |
| rebalance_count | {int(float(m.get("rebalance_count") or 0))} |
| avg_holding_days | {_num(m.get("avg_holding_days"), 2)} |
| rebalance_turnover_mean | {_pct(m.get("rebalance_turnover_mean"))} |
| is_rebalance_days | {winner["is_rebalance_days"]} |
| MC p_sharpe | {_mc_p(winner)} |

## 最近持仓（{hold_stats["last_date"]}，n={hold_stats["n_held"]}）

{hold_md}

详细搜索过程见 `runs/_research_qlib158_trendstock_stage2/stage2_summary.md`。
"""
    (dest / f"REPORT_F0_trendstock_{mom}_{legs}.md").write_text(body, encoding="utf-8")
    print(f"[publish] {dest}")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--mc-sims", type=int, default=1000)
    args = ap.parse_args()

    irs = _load_irs()
    data_map = _load_ohlcv_src(SRC_RUN)
    panel = _build_panel(data_map)
    close = panel["close"]
    fwd = _forward_returns(close)
    print(f"[panel] symbols={close.shape[1]} bars={close.shape[0]}")

    ids = Registry().list(zoo="qlib158")
    scores: dict[str, pd.DataFrame] = {}
    ic_rows: list[dict] = []
    for aid in ids:
        try:
            raw = Registry().compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {aid}: {exc}")
            continue
        if raw is None or raw.empty:
            continue
        raw = raw.reindex(index=close.index, columns=close.columns)
        stats = _pool_ic(raw, fwd)
        sign = -1.0 if stats["ic_mean"] < 0 else 1.0
        scores[aid] = _smooth_cs(raw * sign if sign < 0 else raw)
        ic_rows.append({"id": aid, "sign": sign, **stats})
    ic_table = pd.DataFrame(ic_rows).sort_values("ir", ascending=False)
    print(f"[scores] {len(scores)} factors")

    # Pool-IR top-ups so pool-only factors (qtld30/vma60/...) get a real weight.
    irs_eff = dict(irs)
    for row in ic_table.itertuples(index=False):
        if row.n < 120:
            continue
        irs_eff.setdefault(row.id, max(float(row.ir), 0.10))

    # Confirm shortlist: stage-1 top pairs + pool-IR singles + baselines.
    pool_singles = [
        str(r.id) for r in ic_table.itertuples(index=False)
        if r.n >= 120 and r.ir > 0 and str(r.id) not in MOM_LEGS and str(r.id) not in BANNED_CONFIRM
    ][:8]
    combos: list[list[str]] = [[]] + [list(NAIVE_CONFIRM)] + [list(v) for v in REFERENCE_COMBOS.values()]
    combos += [list(c) for c in STAGE1_TOP]
    combos += [[a] for a in pool_singles]
    seen: set[tuple[str, ...]] = set()
    shortlist: list[list[str]] = []
    for c in combos:
        k = tuple(sorted(c))
        if k in seen:
            continue
        seen.add(k)
        shortlist.append(c)
    print(f"[shortlist] confirm sets={len(shortlist)}  pool singles={pool_singles}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    t0 = time.time()

    def _rank(mom: str, c: list[str]) -> dict:
        label = f"{mom.replace('qlib158_', '')}__{_combo_label(tuple(c))}"
        run_dir = OUT_ROOT / f"screen_{label}"
        mpath = run_dir / "artifacts" / "metrics.csv"
        if mpath.exists():
            m = pd.read_csv(mpath).iloc[0].to_dict()
        else:
            m = _run(mom, c, scores, irs_eff, data_map, run_dir, mc_sims=0)["metrics"]
        row = {
            "mom": mom.replace("qlib158_", ""),
            "confirm": _combo_label(tuple(c)),
            "confirm_ids": c,
            "label": label,
            "total_return": float(m.get("total_return", 0.0)),
            "excess_return": float(m.get("excess_return", 0.0)),
            "sharpe": float(m.get("sharpe", 0.0)),
            "max_drawdown": float(m.get("max_drawdown", 0.0)),
            "benchmark_return": float(m.get("benchmark_return", 0.0)),
            "phase": "confirm" if mom == BASE_MOM else "momentum",
        }
        rows.append(row)
        print(
            f"  engine {len(rows)} {label} excess={row['excess_return']:.4%} "
            f"sharpe={row['sharpe']:.3f} elapsed={time.time() - t0:.0f}s"
        )
        return row

    print("[phase-a] confirm ranking with std30 (engine, mc=0)")
    for c in shortlist:
        _rank(BASE_MOM, c)

    phase_a = pd.DataFrame(rows).sort_values(
        ["excess_return", "sharpe"], ascending=False
    ).reset_index(drop=True)
    top_confirms = [list(x) for x in phase_a.head(3)["confirm_ids"]]
    print("[phase-b] momentum ablation on top confirm sets")
    for c in top_confirms:
        for mom in MOM_LEGS:
            if mom == BASE_MOM:
                continue
            _rank(mom, c)

    df = pd.DataFrame(rows).sort_values(
        ["excess_return", "sharpe"], ascending=False
    ).reset_index(drop=True)
    df.to_csv(OUT_ROOT / "stage2_rank.csv", index=False, encoding="utf-8-sig")
    print(f"[engine-rank] best={df.iloc[0]['label']} excess={df.iloc[0]['excess_return']:.4%}")

    # MC-verify the top rows (dedupe by label) plus the base-std30 comparators.
    verify: list[tuple[str, list[str]]] = []
    seen_lab: set[str] = set()
    for _, row in df.head(max(args.top_k, 1)).iterrows():
        lab = f"{row.mom}__{row.confirm}"
        if lab in seen_lab:
            continue
        seen_lab.add(lab)
        verify.append(("qlib158_" + str(row.mom), list(row.confirm_ids)))
    for c in ([], list(NAIVE_CONFIRM), list(REFERENCE_COMBOS["copper_kmid2+max60"]),
              list(REFERENCE_COMBOS["memory_kmid+ksft2"])):
        lab = f"{BASE_MOM.replace('qlib158_', '')}__{_combo_label(tuple(c))}"
        if lab in seen_lab:
            continue
        seen_lab.add(lab)
        verify.append((BASE_MOM, c))

    verified: list[dict] = []
    for mom, c in verify:
        lab = f"{mom.replace('qlib158_', '')}__{_combo_label(tuple(c))}"
        print(f"[mc] {lab}")
        res = _run(mom, c, scores, irs_eff, data_map, OUT_ROOT / f"full_{lab}", args.mc_sims)
        verified.append(res)
        print(
            f"   excess={_pct(res['metrics'].get('excess_return'))} "
            f"sharpe={_num(res['metrics'].get('sharpe'))} mc_p={_mc_p(res)}"
        )

    verified.sort(key=lambda x: float(x["metrics"]["excess_return"]), reverse=True)
    best = verified[0]
    print(f"[stage3] F1 MAX veto on {best['label']}")
    f1 = _run(
        best["mom_id"],
        list(best["confirm_ids"]),
        scores,
        irs_eff,
        data_map,
        OUT_ROOT / f"full_{best['label']}_F1",
        args.mc_sims,
        veto_max=True,
    )
    d_ex = float(f1["metrics"]["excess_return"]) - float(best["metrics"]["excess_return"])
    d_sh = float(f1["metrics"]["sharpe"]) - float(best["metrics"]["sharpe"])
    use_f1 = d_ex > 0.01 and d_sh >= 0.0
    winner = f1 if use_f1 else best
    print(f"  F1 Δexcess={_pct(d_ex)} Δsharpe={_num(d_sh)} → {'F1' if use_f1 else 'F0'}")

    # Momentum-leg ablation table around the winning confirm set.
    winner_confirm = tuple(sorted(best["confirm_ids"]))
    abl = df[df["confirm_ids"].apply(lambda x: tuple(sorted(x)) == winner_confirm)]
    abl = abl.sort_values("excess_return", ascending=False)

    name_map = _name_map()
    hold_md, hold_stats = _holdings_md(Path(winner["run_dir"]), name_map)
    out_lines = [
        "# 趋势股 BK1715 — 二段式因子组合搜索（引擎排名 + 动量腿消融）",
        "",
        "**研究/回测，非实盘建议。**",
        "",
        f"- 标的池：{UNIVERSE_DESC}；区间 {START_DATE} ~ {END_DATE}",
        f"- 引擎排名组合数：{len(df)}（{len(shortlist)} 个确认集，阶段B对前3名做动量腿消融）",
        f"- 动量腿候选：{', '.join(m.replace('qlib158_', '') for m in MOM_LEGS)}",
        "",
        "## 引擎排名 Top15",
        "",
        "| # | mom | confirm | excess | total | sharpe | max_dd |",
        "|--:|-----|---------|-------:|------:|-------:|-------:|",
    ]
    for i, row in enumerate(df.head(15).itertuples(index=False), 1):
        out_lines.append(
            f"| {i} | {row.mom} | `{row.confirm}` | {_pct(row.excess_return)} | "
            f"{_pct(row.total_return)} | {_num(row.sharpe)} | {_pct(row.max_drawdown)} |"
        )
    out_lines += [
        "",
        "## 蒙特卡洛复核",
        "",
        "| label | excess | total | sharpe | max_dd | MC p_sharpe |",
        "|-------|-------:|------:|-------:|-------:|------------:|",
    ]
    for v in verified:
        m = v["metrics"]
        out_lines.append(
            f"| `{v['label']}` | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_mc_p(v)} |"
        )
    out_lines += [
        f"| `{f1['label']}` | {_pct(f1['metrics']['excess_return'])} | "
        f"{_pct(f1['metrics']['total_return'])} | {_num(f1['metrics']['sharpe'])} | "
        f"{_pct(f1['metrics']['max_drawdown'])} | {_mc_p(f1)} |",
        "",
        "## 动量腿消融（确认集固定为最优）",
        "",
        "| mom | excess | total | sharpe | max_dd |",
        "|-----|-------:|------:|-------:|-------:|",
    ]
    for row in abl.itertuples(index=False):
        out_lines.append(
            f"| {row.mom} | {_pct(row.excess_return)} | {_pct(row.total_return)} | "
            f"{_num(row.sharpe)} | {_pct(row.max_drawdown)} |"
        )
    out_lines += [
        "",
        "## 结论",
        "",
        f"- 最优组合：动量 `{best['mom_id']}` + 确认 `{_combo_label(tuple(best['confirm_ids']))}`",
        f"- excess={_pct(winner['metrics']['excess_return'])}，total={_pct(winner['metrics']['total_return'])}，"
        f"sharpe={_num(winner['metrics']['sharpe'])}，max_dd={_pct(winner['metrics']['max_drawdown'])}，"
        f"MC p={_mc_p(winner)}",
        f"- 纯动量 std30 对照：excess="
        f"{_pct(next((v['metrics']['excess_return'] for v in verified if v['label'] == 'std30__pure_std30'), 0.0))}",
        f"- F1 MAX 否决：Δexcess={_pct(d_ex)}，Δsharpe={_num(d_sh)} → "
        + ("采用 F1" if use_f1 else "回退 F0"),
        f"- 最近持仓日 {hold_stats['last_date']}；n={hold_stats['n_held']}",
        "",
        hold_md,
        "",
    ]
    SUMMARY_OUT.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    dest = _publish(winner, name_map)
    print(f"[done] {dest.name}")


if __name__ == "__main__":
    raise SystemExit(main())
