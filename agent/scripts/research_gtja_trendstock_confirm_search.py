"""GTJA191 alive-factor confirm-leg search on 趋势股 BK1715.

The 金属铜 / 存储芯片 / 趋势股 F0' lines all searched confirm legs inside
``qlib158``. This run flips the factor source: the candidate universe is the
**存活(survival) GTJA191** set — the 8 names that survived the CSI300
2023-2026 rebench (``ic_mean>0.02``, ``ic+>=0.55``, ``|t|>2``) — and the
question is which combination makes the best *confirm leg* on this board.

Design:

1. compute every alive GTJA191 factor once on the BK1715 panel, orient it by
   the pool's own rank-IC sign, cross-sectionally smooth it;
2. fast-screen ``momentum leg x confirm set`` (singles + pairs) with the
   in-memory path;
3. re-rank the shortlist with the **real market engine** (the lesson from the
   qlib158 round: on this pool the fast path inverts);
4. ablate the momentum leg on the best confirm sets;
5. Monte-Carlo the winner, publish it as a ``runs/<id>`` backtest record.

The published qlib158 winner (``vsumn60 + cord30+ksft``) is re-run through the
same harness as the reference row, so both factor sources are directly
comparable.

Run from ``agent/``:
    python scripts/research_gtja_trendstock_confirm_search.py --top-k 12
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
    END_DATE,
    FALLBACK_IR,
    INITIAL_CASH,
    PANEL,
    SRC_RUN,
    START_DATE,
    UNIVERSE_DESC,
    _compute_all,
    _forward_returns,
    _holdings_md,
    _mc_p,
    _name_map,
    _num,
    _pct,
)
from scripts.research_sector_confirm_search import _load_ohlcv as _load_ohlcv_src  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402

OUT_ROOT = _AGENT_ROOT / "runs" / "_research_gtja_trendstock_confirm_search"
SUMMARY_OUT = OUT_ROOT / "gtja_summary.md"
PROP_OUT = _AGENT_ROOT / "prop_gtja191_趋势股_最优.md"

ZOO = "gtja191"
PREFIX = f"{ZOO}_"
REFERENCE_COMBO = ("qlib158_vsumn60", ("qlib158_cord30", "qlib158_ksft"))
REFERENCE_LABEL = "vsumn60 + cord30+ksft (qlib158 incumbent)"
MOM_ORDER_CAP = 4  # max shortlist rows per momentum leg -> keep the engine grid diverse


# ----------------------------------------------------------------------------- #
# factor bookkeeping
# ----------------------------------------------------------------------------- #
def _survival_rows() -> dict[str, dict]:
    path = survival_path()
    if not path.is_file():
        raise SystemExit(f"survival store missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or payload
    return {
        str(k): v
        for k, v in alphas.items()
        if isinstance(v, dict) and v.get("zoo") == ZOO
    }


def _ids_by_category(rows: dict[str, dict], category: str) -> list[str]:
    out = [
        aid
        for aid, row in rows.items()
        if row.get("category") == category or row.get("alive_status") == category
    ]
    return sorted(out, key=lambda a: -(float(rows[a].get("ir") or 0.0)))


def _irs(rows: dict[str, dict], ids: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for aid in ids:
        row = rows.get(aid) or {}
        out[aid] = float(row.get("ir") or 0.12)
    # qlib158 reference legs are not in the survival store any more -> fallback IR.
    for aid, ir in FALLBACK_IR.items():
        out.setdefault(aid, ir)
    return out


def _lab(ids: tuple[str, ...] | list[str]) -> str:
    if not ids:
        return "pure"
    return "+".join(a.replace(PREFIX, "") for a in ids)


def _build_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """OHLCV + synthetic amount (the lake stores amount as NaN)."""
    cols = sorted(data_map)
    close = pd.DataFrame({c: data_map[c]["close"] for c in cols})
    volume = pd.DataFrame({c: data_map[c]["volume"] for c in cols})
    return {
        "close": close,
        "open": pd.DataFrame({c: data_map[c]["open"] for c in cols}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in cols}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in cols}),
        "volume": volume,
        "amount": close * volume,
    }


# ----------------------------------------------------------------------------- #
# fast path
# ----------------------------------------------------------------------------- #
def _fast(mom_id, confirm_ids, scores, irs, close) -> dict:
    ok = _confirm_ok(tuple(confirm_ids), scores, irs, close)
    weights, n_reb = _build_weights(scores[mom_id], ok, close)
    m = _fast_metrics(weights, close)
    return {
        "mom": mom_id.replace(PREFIX, ""),
        "mom_id": mom_id,
        "confirm": _lab(confirm_ids),
        "confirm_ids": list(confirm_ids),
        "n_confirm": len(confirm_ids),
        "is_rebalance_days": n_reb,
        "total_return": float(m.get("total_return", 0.0)),
        "excess_return": float(m.get("excess_return", 0.0)),
        "sharpe": float(m.get("sharpe", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "benchmark_return": float(m.get("benchmark_return", 0.0)),
        "rebalance_turnover_mean": float(m.get("rebalance_turnover_mean", 0.0) or 0.0),
    }


# ----------------------------------------------------------------------------- #
# market engine
# ----------------------------------------------------------------------------- #
class _Engine:
    def __init__(self, mom_id, confirm_ids, scores, irs):
        self.mom_id = mom_id
        self.confirm_ids = list(confirm_ids)
        self.alpha_ids = [mom_id] + self.confirm_ids
        self.scores = scores
        self.irs = irs
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).sort_index()
        mom = self.scores[self.mom_id].reindex(index=close.index, columns=close.columns)
        ok = _confirm_ok(tuple(self.confirm_ids), self.scores, self.irs, close)
        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _run(mom_id, confirm_ids, scores, irs, data_map, run_dir: Path, mc_sims: int) -> dict:
    label = f"{mom_id.replace(PREFIX, '')}__{_lab(tuple(confirm_ids))}"
    metrics_path = run_dir / "artifacts" / "metrics.csv"
    val_path = run_dir / "artifacts" / "validation.json"
    if metrics_path.exists() and (mc_sims <= 0 or val_path.exists()):
        metrics = pd.read_csv(metrics_path).iloc[0].to_dict()
        validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
        print(f"  [reuse] {label}")
        return {
            "label": label,
            "mom_id": mom_id,
            "confirm_ids": list(confirm_ids),
            "metrics": metrics,
            "validation": validation,
            "is_rebalance_days": int(metrics.get("rebalance_count") or 0),
            "run_dir": str(run_dir),
        }
    if run_dir.exists():
        run_dir = run_dir.with_name(f"{run_dir.name}_r{int(time.time()) % 100000}")
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

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
        "alphas": [mom_id] + list(confirm_ids),
        "parameters": {
            "strategy": f"gtja191_trendstock_{label}",
            "panel": PANEL,
            "mom_id": mom_id,
            "confirm_ids": list(confirm_ids),
            "zoo": ZOO,
        },
        "_run_card_effective_sources": ["local_artifacts"],
    }
    if mc_sims >= 1:
        cfg["validation"] = {"monte_carlo": {"n_simulations": mc_sims}}
    (run_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    sig = _Engine(mom_id, confirm_ids, scores, irs)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    engine.run_backtest(
        cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=calc_bars_per_year("1D", "tushare")
    )
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "label": label,
        "mom_id": mom_id,
        "confirm_ids": list(confirm_ids),
        "metrics": metrics,
        "validation": validation,
        "is_rebalance_days": sig.last_is_rebalance_days,
        "run_dir": str(run_dir),
    }


# ----------------------------------------------------------------------------- #
# publish
# ----------------------------------------------------------------------------- #
def _publish(winner: dict, name_map: dict[str, str], *, reference: dict | None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mom = winner["mom_id"].replace(PREFIX, "")
    legs = "_".join(a.replace(PREFIX, "") for a in winner["confirm_ids"]) or "pure"
    run_id = f"{stamp}_05_F0_trendstock_gtja_{mom}__{legs}"
    dest = _AGENT_ROOT / "runs" / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(Path(winner["run_dir"]), dest)
    title = f"趋势股BK1715 F0' GTJA191存活 {mom}+{legs} — 国泰君安存活因子最优确认脚"
    prompt = (
        f"市场扫描第2板块 趋势股 BK1715（80只，{START_DATE}~{END_DATE}）全新回测："
        f"因子池换成国泰君安 GTJA191 存活因子（CSI300 2023-2026 存活 8 个），"
        f"动量腿 {mom} + IR加权硬确认 {legs}，两段式搜索（快筛→引擎排名→动量腿消融→MC）选出最优确认脚。"
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
                    "zoo": ZOO,
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
        json.dumps({"status": "success", "source": "research_gtja_trendstock_confirm_search"}, ensure_ascii=False),
        encoding="utf-8",
    )
    m = winner["metrics"]
    hold_md, hold_stats = _holdings_md(dest, name_map)
    ref_md = ""
    if reference:
        rm = reference["metrics"]
        ref_md = (
            f"\n## 对照：qlib158 已发布最优\n\n"
            f"| 指标 | qlib158 `{REFERENCE_LABEL}` | 本次 GTJA191 |\n|---|---:|---:|\n"
            f"| total_return | {_pct(rm.get('total_return'))} | {_pct(m.get('total_return'))} |\n"
            f"| excess_return | {_pct(rm.get('excess_return'))} | {_pct(m.get('excess_return'))} |\n"
            f"| sharpe | {_num(rm.get('sharpe'))} | {_num(m.get('sharpe'))} |\n"
            f"| max_drawdown | {_pct(rm.get('max_drawdown'))} | {_pct(m.get('max_drawdown'))} |\n"
        )
    body = f"""# {title}

**研究/回测，非实盘建议。**

## 设定

- 标的池：{UNIVERSE_DESC}
- 区间：{START_DATE} ~ {END_DATE}；source=lake（本地日线）；周频首个交易日调仓
- 因子池：GTJA191 **存活**因子（survival store `zoo=gtja191`，`category=alive`，CSI300 2023-2026 rebench）
- 动量腿：`{winner["mom_id"]}`；确认腿：`{", ".join(a.replace(PREFIX, "") for a in winner["confirm_ids"])}`（按存活 IR 加权，C≥40% 分位才允许开新仓）
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
{ref_md}
## 最近持仓（{hold_stats["last_date"]}，n={hold_stats["n_held"]}）

{hold_md}

完整搜索过程见 `runs/_research_gtja_trendstock_confirm_search/gtja_summary.md`。
"""
    (dest / f"REPORT_F0_trendstock_gtja_{mom}_{legs}.md").write_text(body, encoding="utf-8")
    print(f"[publish] {dest}")
    return dest


# ----------------------------------------------------------------------------- #
# main
# ----------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top-k", type=int, default=12)
    ap.add_argument("--engine-mc", type=int, default=0)
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument("--max-confirm", type=int, default=2)
    args = ap.parse_args()

    rows = _survival_rows()
    alive = _ids_by_category(rows, "alive")
    reversed_ids = _ids_by_category(rows, "reversed")
    dead = [k for k, v in rows.items() if v.get("category") == "dead"]
    print(f"[survival] zoo={ZOO} alive={len(alive)} reversed={len(reversed_ids)} dead={len(dead)}")
    if not alive:
        raise SystemExit("no alive GTJA191 factor")
    for aid in alive:
        print(f"  alive {aid} ir={rows[aid].get('ir')} ic={rows[aid].get('ic_mean')}")

    data_map = _load_ohlcv_src(SRC_RUN)
    panel = _build_panel(data_map)
    close = panel["close"]
    fwd = _forward_returns(close)
    print(f"[panel] symbols={close.shape[1]} bars={close.shape[0]} {close.index[0].date()}~{close.index[-1].date()}")

    irs = _irs(rows, alive + reversed_ids)

    # --- compute: alive GTJA191 + the qlib158 reference legs (harness self-check)
    ref_legs = [REFERENCE_COMBO[0], *REFERENCE_COMBO[1]]
    compute_ids = alive + ref_legs
    t0 = time.time()
    scores, ic = _compute_all(panel, compute_ids, fwd)
    print(f"[compute] {len(scores)} factors in {time.time() - t0:.1f}s")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    ic.to_csv(OUT_ROOT / "gtja_pool_ic.csv", index=False, encoding="utf-8")

    gtja_scores = [a for a in alive if a in scores]
    print(f"[orient] usable GTJA factors: {len(gtja_scores)}/{len(alive)}")

    # --- momentum legs: every usable GTJA factor + the qlib158 incumbent
    mom_legs = [a for a in gtja_scores]
    ref_mom = REFERENCE_COMBO[0]
    if ref_mom in scores:
        mom_legs.append(ref_mom)

    # --- confirm candidates: singles + pairs among alive GTJA
    confirm_sets: list[tuple[str, ...]] = [()]
    for n in range(1, args.max_confirm + 1):
        confirm_sets.extend(itertools.combinations(gtja_scores, n))
    print(f"[fast] {len(mom_legs)} momentum legs x {len(confirm_sets)} confirm sets = {len(mom_legs) * len(confirm_sets)}")

    fast_rows: list[dict] = []
    t0 = time.time()
    for mom in mom_legs:
        for c in confirm_sets:
            fast_rows.append(_fast(mom, c, scores, irs, close))
        print(f"  fast {mom} done ({time.time() - t0:.1f}s)")
    fast = pd.DataFrame(fast_rows)
    fast.to_csv(OUT_ROOT / "gtja_fast_screen.csv", index=False, encoding="utf-8")

    # pure-momentum baseline per leg -> marginal value of the confirm leg
    pure = fast[fast.n_confirm == 0].set_index("mom_id")
    fast["d_excess_vs_pure"] = fast.apply(
        lambda r: r.excess_return - float(pure.loc[r.mom_id, "excess_return"]), axis=1
    )
    fast.sort_values(["excess_return", "sharpe"], ascending=False).to_csv(
        OUT_ROOT / "gtja_fast_screen.csv", index=False, encoding="utf-8"
    )
    print("[fast] top 10:")
    print(
        fast.sort_values(["excess_return", "sharpe"], ascending=False)
        .head(10)[["mom", "confirm", "excess_return", "sharpe", "max_drawdown", "d_excess_vs_pure"]]
        .to_string(index=False)
    )

    # --- engine rank shortlist (diverse across momentum legs)
    ranked = fast[(fast.n_confirm >= 1) & (fast.apply(lambda r: r.mom_id not in r.confirm_ids, axis=1))].sort_values(
        ["excess_return", "sharpe"], ascending=False
    )
    shortlist: list[dict] = []
    per_mom: dict[str, int] = {}
    for _, r in ranked.iterrows():
        if per_mom.get(r.mom_id, 0) >= MOM_ORDER_CAP:
            continue
        shortlist.append(
            {"mom_id": r.mom_id, "confirm_ids": list(r.confirm_ids), "fast_excess": r.excess_return}
        )
        per_mom[r.mom_id] = per_mom.get(r.mom_id, 0) + 1
        if len(shortlist) >= args.top_k:
            break
    print(f"[engine] shortlist {len(shortlist)}")

    engine_rows: list[dict] = []
    t0 = time.time()
    for i, item in enumerate(shortlist, 1):
        run_dir = OUT_ROOT / f"engine_{item['mom_id'].replace(PREFIX, '')}__{_lab(item['confirm_ids'])}"
        res = _run(item["mom_id"], item["confirm_ids"], scores, irs, data_map, run_dir, args.engine_mc)
        m = res["metrics"]
        engine_rows.append(
            {
                "mom": item["mom_id"].replace(PREFIX, ""),
                "mom_id": item["mom_id"],
                "confirm": _lab(item["confirm_ids"]),
                "confirm_ids": item["confirm_ids"],
                "fast_excess": item["fast_excess"],
                "total_return": float(m.get("total_return", 0.0)),
                "excess_return": float(m.get("excess_return", 0.0)),
                "sharpe": float(m.get("sharpe", 0.0)),
                "max_drawdown": float(m.get("max_drawdown", 0.0)),
                "trade_count": int(float(m.get("trade_count") or 0)),
                "run_dir": res["run_dir"],
            }
        )
        print(
            f"  engine {i}/{len(shortlist)} {engine_rows[-1]['mom']}__{engine_rows[-1]['confirm']} "
            f"excess={engine_rows[-1]['excess_return']:+.4f} sharpe={engine_rows[-1]['sharpe']:.3f} "
            f"({time.time() - t0:.0f}s)"
        )
    eng = pd.DataFrame(engine_rows).sort_values(["excess_return", "sharpe"], ascending=False)
    eng.to_csv(OUT_ROOT / "gtja_engine_rank.csv", index=False, encoding="utf-8")
    print("[engine] top 8:")
    print(eng.head(8)[["mom", "confirm", "fast_excess", "excess_return", "sharpe", "max_drawdown"]].to_string(index=False))

    # --- momentum ablation on the best confirm sets
    top_confirms: list[list[str]] = []
    for _, r in eng.iterrows():
        if list(r.confirm_ids) not in top_confirms:
            top_confirms.append(list(r.confirm_ids))
        if len(top_confirms) >= 3:
            break
    print(f"[ablate] confirm sets: {[_lab(c) for c in top_confirms]}")
    abl_rows: list[dict] = []
    for c in top_confirms:
        for mom in mom_legs:
            run_dir = OUT_ROOT / f"ablate_{mom.replace(PREFIX, '')}__{_lab(tuple(c))}"
            res = _run(mom, c, scores, irs, data_map, run_dir, args.engine_mc)
            m = res["metrics"]
            abl_rows.append(
                {
                    "mom": mom.replace(PREFIX, ""),
                    "mom_id": mom,
                    "confirm": _lab(tuple(c)),
                    "confirm_ids": list(c),
                    "total_return": float(m.get("total_return", 0.0)),
                    "excess_return": float(m.get("excess_return", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "run_dir": res["run_dir"],
                }
            )
            print(
                f"  ablate {abl_rows[-1]['mom']}__{abl_rows[-1]['confirm']} "
                f"excess={abl_rows[-1]['excess_return']:+.4f}"
            )
    abl = pd.DataFrame(abl_rows).drop_duplicates(subset=["mom_id", "confirm"]).sort_values(
        ["excess_return", "sharpe"], ascending=False
    )
    abl.to_csv(OUT_ROOT / "gtja_mom_ablation.csv", index=False, encoding="utf-8")

    # --- reference (qlib158 incumbent) through the same harness
    reference = None
    if ref_mom in scores:
        ref_dir = OUT_ROOT / f"engine_{ref_mom.replace(PREFIX, '')}__{_lab(REFERENCE_COMBO[1])}"
        reference = _run(ref_mom, list(REFERENCE_COMBO[1]), scores, irs, data_map, ref_dir, 0)
        rm = reference["metrics"]
        print(
            f"[reference] {REFERENCE_LABEL}: total={float(rm['total_return']):+.4f} "
            f"excess={float(rm['excess_return']):+.4f} sharpe={float(rm['sharpe']):.3f}"
        )

    # --- winner + MC
    combined = pd.concat(
        [eng[["mom", "mom_id", "confirm", "confirm_ids", "excess_return", "sharpe", "max_drawdown"]], abl[["mom", "mom_id", "confirm", "confirm_ids", "excess_return", "sharpe", "max_drawdown"]]],
        ignore_index=True,
    ).drop_duplicates(subset=["mom_id", "confirm"])
    combined = combined.sort_values(["excess_return", "sharpe"], ascending=False)
    winner_row = combined.iloc[0]
    print(f"[winner] {winner_row.mom}__{winner_row.confirm} excess={winner_row.excess_return:+.4f}")

    win = _run(
        winner_row.mom_id,
        list(winner_row.confirm_ids),
        scores,
        irs,
        data_map,
        OUT_ROOT / f"final_{winner_row.mom}__{winner_row.confirm}",
        args.mc_sims,
    )
    runner_row = combined.iloc[1]
    runner = _run(
        runner_row.mom_id,
        list(runner_row.confirm_ids),
        scores,
        irs,
        data_map,
        OUT_ROOT / f"final2_{runner_row.mom}__{runner_row.confirm}",
        args.mc_sims,
    )
    print(f"[mc] winner p={_mc_p(win)} runner p={_mc_p(runner)}")

    # --- reversed-factor sensitivity (可选，不参与主选)
    sens_rows: list[dict] = []
    if reversed_ids:
        rev_ids = [a for a in reversed_ids]
        rev_scores, rev_ic = _compute_all(panel, rev_ids, fwd)
        rev_scores.update({k: v for k, v in scores.items()})
        rev_ic.to_csv(OUT_ROOT / "gtja_reversed_ic.csv", index=False, encoding="utf-8")
        best_rev = (
            rev_ic.sort_values("ir", ascending=False).iloc[0]["id"] if not rev_ic.empty else None
        )
        if best_rev:
            for base in [list(winner_row.confirm_ids), list(runner_row.confirm_ids)]:
                cand = base + [best_rev]
                res = _run(
                    winner_row.mom_id,
                    cand,
                    rev_scores,
                    irs,
                    data_map,
                    OUT_ROOT / f"sens_{winner_row.mom}__{_lab(tuple(cand))}",
                    0,
                )
                m = res["metrics"]
                sens_rows.append(
                    {
                        "mom": winner_row.mom,
                        "confirm": _lab(tuple(cand)),
                        "added": best_rev,
                        "total_return": float(m.get("total_return", 0.0)),
                        "excess_return": float(m.get("excess_return", 0.0)),
                        "sharpe": float(m.get("sharpe", 0.0)),
                        "delta_excess": float(m.get("excess_return", 0.0))
                        - float(win["metrics"].get("excess_return", 0.0)),
                        "run_dir": res["run_dir"],
                    }
                )
                print(f"  [sens] +{best_rev}: excess={sens_rows[-1]['excess_return']:+.4f} d={sens_rows[-1]['delta_excess']:+.4f}")

    # --- publish
    dest = _publish(win, _name_map(), reference=reference)

    # --- summary
    def _row_line(r) -> str:
        return (
            f"| `{r.mom}` | `{r.confirm}` | {_pct(r.excess_return)} | {_pct(getattr(r, 'total_return', 0))} | "
            f"{_num(r.sharpe)} | {_pct(r.max_drawdown)} |"
        )

    ic_md = "\n".join(
        f"| `{r.id.replace(PREFIX, '')}` | {r.ic_mean:+.4f} | {r.ir:+.3f} | {r.pos_ratio:.2f} | {int(r.sign):+d} |"
        for r in ic.itertuples()
    )
    eng_md = "\n".join(_row_line(r) for r in eng.head(10).itertuples())
    abl_md = "\n".join(_row_line(r) for r in abl.head(12).itertuples())
    wm = win["metrics"]
    ref_line = ""
    if reference:
        rm = reference["metrics"]
        ref_line = (
            f"| qlib158 `{REFERENCE_LABEL}` | {_pct(rm.get('excess_return'))} | {_pct(rm.get('total_return'))} | "
            f"{_num(rm.get('sharpe'))} | {_pct(rm.get('max_drawdown'))} |"
        )
    sens_md = "\n".join(
        f"| `{r['confirm']}` | {_pct(r['excess_return'])} | {_pct(r['delta_excess'])} | {_num(r['sharpe'])} |"
        for r in sens_rows
    )

    summary = f"""# GTJA191 存活因子 · 趋势股 BK1715 最优确认脚搜索

**研究/回测，非实盘建议。** 因子池换成国泰君安 GTJA191 **存活因子**（survival store，CSI300 2023-2026 rebench，alive={len(alive)}）。

- 标的池：{UNIVERSE_DESC}
- 区间：{START_DATE} ~ {END_DATE}（{close.shape[0]} 根日线，{close.shape[1]} 只成分，lake 本地数据）
- 过程：周频首日调仓 / N=6 等权 / 保留带 Top9 / 换手帽 20% / 最小持有 5 日 / 止损 15% / 止盈 35% / MA20 择时
- 确认腿打分：存活 IR 加权平均分位，C≥40% 分位才允许开新仓

## 1. 存活因子在本池的截面表现（rank IC，方向已按池内 IC 定向）

| id | ic_mean | ir | ic>0 占比 | sign |
|---|---:|---:|---:|---:|
{ic_md}

## 2. 最优结果（市场引擎，非快筛）

| 动量腿 | 确认腿 | excess | total | sharpe | max_dd |
|---|---|---:|---:|---:|---:|
| **`{winner_row.mom}`** | **`{winner_row.confirm}`** | **{_pct(wm.get('excess_return'))}** | **{_pct(wm.get('total_return'))}** | **{_num(wm.get('sharpe'))}** | **{_pct(wm.get('max_drawdown'))}** |
{eng_md}
{ref_line}

MC p_sharpe（{args.mc_sims} 次）：**{_mc_p(win)}**；次优 `{runner_row.mom}__{runner_row.confirm}` p={_mc_p(runner)}。

## 3. 动量腿消融（同一确认腿换排序腿）

| 动量腿 | 确认腿 | excess | total | sharpe | max_dd |
|---|---|---:|---:|---:|---:|
{abl_md}

## 4. 快筛 vs 引擎（再次验证快筛不可信）

引擎前 10 里，快筛 excess 与引擎 excess 的符号/排序对比见 `gtja_engine_rank.csv` 的 `fast_excess` 与 `excess_return` 两列。

## 5. 反向(reversed)因子追加敏感性

| 确认腿 | excess | Δexcess | sharpe |
|---|---:|---:|---:|
{sens_md or "| — | — | — | — |"}

## 6. 发布

- run 记录：`runs/{dest.name}`
- 报告：`{dest.name}/REPORT_F0_trendstock_gtja_*.md`
- 复现提示词：`{PROP_OUT.name}`
"""
    SUMMARY_OUT.write_text(summary, encoding="utf-8")
    print(f"[summary] {SUMMARY_OUT}")

    prop = f"""# prop · GTJA191 存活因子 趋势股 BK1715 最优回测复现

> 研究/回测，非实盘建议。

## 一句话

在 趋势股 BK1715（80 只成分，{START_DATE}~{END_DATE}）上，用**国泰君安 GTJA191 存活因子**（survival store alive {len(alive)} 个）重做 F0' 流程，
最优是 动量腿 `{winner_row.mom}` + 确认腿 `{_lab(winner_row.confirm_ids)}`：
excess **{_pct(wm.get('excess_return'))}**、total **{_pct(wm.get('total_return'))}**、sharpe **{_num(wm.get('sharpe'))}**、max_dd **{_pct(wm.get('max_drawdown'))}**。

## 复现步骤

```bash
cd agent
python scripts/research_gtja_trendstock_confirm_search.py --top-k 12 --mc-sims 1000
```

## 关键参数

- 因子：`{", ".join(winner_row.confirm_ids)}`（确认腿，存活 IR 加权）+ `{winner_row.mom_id}`（动量腿）
- 组合：N=6 等权，保留带 Top9，换手帽 20%，最小持有 5 日，峰值回撤 15% 止损，止盈 35%，MA20 择时
- 确认门槛：加权分位 ≥ 40%

## 交付

- 回测记录：`runs/{dest.name}`
- 搜索明细：`runs/_research_gtja_trendstock_confirm_search/`
"""
    PROP_OUT.write_text(prop, encoding="utf-8")
    print(f"[prop] {PROP_OUT}")


if __name__ == "__main__":
    main()
