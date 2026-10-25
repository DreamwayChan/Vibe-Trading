"""Search qlib158 confirm-leg combos on 存储芯片 BK1137, matching F0' 185+070.

Process is the same family as ``F0'最优确认-存储芯片-185+070`` (GTJA 164 +
hard confirm 185/070), but the zoo is Qlib158:

  - momentum sort is fixed at ``qlib158_std30`` (highest-IR momentum survivor,
    analogue of gtja191_164)
  - confirm legs are singles + pairs among the other qlib158 alive factors
  - reversal-theme names (cntn*/cntd60) stay banned, same spirit as GTJA 114
  - weekly first-trading-day / C>=40% IR-weighted / N=6 / band Top9 /
    20% turnover / MA20 timing
  - F1 = best F0 + custom_max_inv bottom-20% new-entry veto; adopt only if
    it actually beats F0 on excess without losing Sharpe

Fast path scores every combo in-memory, then Top-K + pure-mom + IR-naive
pair go through the market engine with Monte Carlo.

Run from agent/:
    python scripts/research_qlib158_memory_confirm_search.py
    python scripts/research_qlib158_memory_confirm_search.py --top-k 5 --mc-sims 1000
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import time
from pathlib import Path

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
from scripts.research_sector_confirm_search import (  # noqa: E402
    _holdings_md,
    _load_ohlcv as _load_ohlcv_src,
)
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402

# Same OHLCV cache as GTJA F0' 185+070 on this panel.
SRC_RUN = _AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe"
OUT_ROOT = _AGENT_ROOT / "runs" / "_research_qlib158_memory_confirm_search"
PROP_OUT = _AGENT_ROOT / "prop_qlib158_存储芯片_最优.md"
REPORT_OUT = OUT_ROOT / "confirm_search_summary.md"
GTJA_F0_REF = _AGENT_ROOT / "runs" / "20260731_tech3_05_F0_memory_185_070"

PANEL = "存储芯片 BK1137"
UNIVERSE_DESC = "东财 存储芯片 BK1137（排除北交所；市值优先最多80只）"
START_DATE = "2024-01-01"
END_DATE = "2026-07-31"
INITIAL_CASH = 1_000_000

# Highest-IR momentum-theme survivor — analogue of gtja191_164 on this zoo.
MOM_ID = "qlib158_std30"
# Reversal-theme survivors: banned from hard confirm / rerank (GTJA 114 rule).
BANNED_CONFIRM = frozenset(
    {
        "qlib158_cntn20",
        "qlib158_cntn30",
        "qlib158_cntn60",
        "qlib158_cntd60",
    }
)
# IR-naive pair: two highest-IR non-mom, non-reversal survivors.
NAIVE_CONFIRM = ("qlib158_cord60", "qlib158_klen")

# User-table IRs (CSI300 2023-2026 after sign-flip repair). Survival store wins.
FALLBACK_IR = {
    "qlib158_cord60": 0.171,
    "qlib158_klen": 0.169,
    "qlib158_std30": 0.162,
    "qlib158_klow": 0.157,
    "qlib158_std10": 0.155,
    "qlib158_ksft2": 0.152,
    "qlib158_std20": 0.152,
    "qlib158_std5": 0.151,
    "qlib158_cord20": 0.147,
    "qlib158_std60": 0.147,
    "qlib158_max20": 0.134,
    "qlib158_ksft": 0.132,
    "qlib158_max30": 0.130,
    "qlib158_cord30": 0.128,
    "qlib158_max10": 0.126,
    "qlib158_cntn60": 0.119,
    "qlib158_max60": 0.117,
    "qlib158_cntn30": 0.116,
    "qlib158_cntn20": 0.113,
    "qlib158_qtlu60": 0.108,
    "qlib158_kmid": 0.107,
    "qlib158_cntd60": 0.105,
    "qlib158_kmid2": 0.095,
}


def _load_alive_irs() -> dict[str, float]:
    """Load qlib158 alive IRs; fall back to the CSI300 repair table."""
    path = survival_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or payload
    out: dict[str, float] = {}
    for aid, row in alphas.items():
        if not str(aid).startswith("qlib158_"):
            continue
        if not isinstance(row, dict) or row.get("category") != "alive":
            continue
        try:
            out[str(aid)] = float(row.get("ir") or FALLBACK_IR.get(aid, 0.10))
        except (TypeError, ValueError):
            out[str(aid)] = float(FALLBACK_IR.get(aid, 0.10))
    for aid, ir in FALLBACK_IR.items():
        out.setdefault(aid, ir)
    return out


def _build_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Wide OHLCV panel; qlib158 survivors only need these columns."""
    cols = sorted(data_map)
    return {
        "close": pd.DataFrame({c: data_map[c]["close"] for c in cols}),
        "open": pd.DataFrame({c: data_map[c]["open"] for c in cols}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in cols}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in cols}),
        "volume": pd.DataFrame({c: data_map[c]["volume"] for c in cols}),
    }


def _precompute_scores(
    panel: dict[str, pd.DataFrame], alive_ids: list[str]
) -> dict[str, pd.DataFrame]:
    """5d-smoothed cross-sectional percentile once per alive factor."""
    reg = Registry()
    scores: dict[str, pd.DataFrame] = {}
    for aid in alive_ids:
        t0 = time.time()
        try:
            raw = reg.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {aid}: {exc}")
            continue
        if raw is None or raw.empty:
            print(f"  [skip] {aid}: empty")
            continue
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = _smooth_cs(raw)
        nn = int(scores[aid].notna().to_numpy().sum())
        print(f"  factor {aid}: non_null={nn} ({time.time() - t0:.2f}s)")
    return scores


def _combo_label(confirm_ids: tuple[str, ...] | list[str]) -> str:
    if not confirm_ids:
        return "pure_std30"
    return "+".join(aid.replace("qlib158_", "") for aid in confirm_ids)


def _score_combo(
    confirm_ids: tuple[str, ...],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    close: pd.DataFrame,
) -> dict:
    mom = scores[MOM_ID]
    ok = _confirm_ok(confirm_ids, scores, irs, close)
    weights, n_reb = _build_weights(mom, ok, close)
    m = _fast_metrics(weights, close)
    return {
        "label": _combo_label(confirm_ids),
        "confirm_ids": list(confirm_ids),
        "n_confirm": len(confirm_ids),
        "is_pure": len(confirm_ids) == 0,
        "is_naive": list(confirm_ids) == list(NAIVE_CONFIRM),
        "is_rebalance_days": n_reb,
        "total_return": float(m.get("total_return", 0.0)),
        "excess_return": float(m.get("excess_return", 0.0)),
        "sharpe": float(m.get("sharpe", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "benchmark_return": float(m.get("benchmark_return", 0.0)),
        "rebalance_turnover_mean": float(m.get("rebalance_turnover_mean", 0.0) or 0.0),
        "irs": {aid: float(irs.get(aid, 0.0)) for aid in confirm_ids},
    }


class _CachedSignalEngine:
    """Full-engine adapter from precomputed smooth scores; optional MAX veto."""

    def __init__(
        self,
        confirm_ids: list[str],
        scores: dict[str, pd.DataFrame],
        irs: dict[str, float],
        *,
        veto_max: bool = False,
    ):
        self.mom_id = MOM_ID
        self.confirm_ids = list(confirm_ids)
        self.alpha_ids = [self.mom_id] + self.confirm_ids
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
        # F1 blocks NEW entries only; keep_candidates still ignore this mask.
        if self.veto_max and "custom_max_inv" in self.scores:
            mx = self.scores["custom_max_inv"].reindex(
                index=close.index, columns=close.columns
            )
            thr = mx.quantile(0.20, axis=1)
            veto_ok = mx.ge(thr, axis=0).fillna(True)
            ok = ok & veto_ok
        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _ensure_max_inv(
    scores: dict[str, pd.DataFrame], data_map: dict[str, pd.DataFrame]
) -> None:
    if "custom_max_inv" in scores:
        return
    panel = _build_panel(data_map)
    raw = Registry().compute("custom_max_inv", panel)
    if raw is None or raw.empty:
        raise SystemExit("custom_max_inv empty")
    raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
    scores["custom_max_inv"] = _smooth_cs(raw)


def _full_engine_run(
    confirm_ids: list[str],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame],
    run_dir: Path,
    mc_sims: int,
    *,
    veto_max: bool = False,
) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    if veto_max:
        _ensure_max_inv(scores, data_map)

    alphas = [MOM_ID] + list(confirm_ids)
    if veto_max:
        alphas.append("custom_max_inv")
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
            "strategy": f"qlib158_{_combo_label(tuple(confirm_ids))}"
            + ("_F1" if veto_max else "_F0"),
            "panel": PANEL,
            "confirm_ids": list(confirm_ids),
            "zoo": "qlib158",
            "mom_id": MOM_ID,
        },
        "_run_card_effective_sources": ["local_artifacts"],
    }
    # Ranking passes skip MC; n_simulations<1 is rejected by monte_carlo_test.
    if mc_sims >= 1:
        cfg["validation"] = {"monte_carlo": {"n_simulations": mc_sims}}
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sig = _CachedSignalEngine(confirm_ids, scores, irs, veto_max=veto_max)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "metrics": metrics,
        "validation": validation,
        "is_rebalance_days": sig.last_is_rebalance_days,
        "confirm_ids": list(confirm_ids),
        "label": _combo_label(tuple(confirm_ids)) + ("_F1" if veto_max else ""),
        "run_dir": str(run_dir),
        "veto_max": veto_max,
        "irs": {aid: float(irs.get(aid, 0.0)) for aid in confirm_ids},
    }


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x, nd: int = 4) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _mc_p(v: dict) -> str:
    mc = (v.get("validation") or {}).get("monte_carlo") or {}
    return _num(mc.get("p_value_sharpe"))


def _gtja_ref_metrics() -> dict | None:
    """Same-panel GTJA F0' 185+070 numbers, for cross-zoo context only."""
    path = GTJA_F0_REF / "artifacts" / "metrics.csv"
    if not path.exists():
        return None
    m = pd.read_csv(path).iloc[0].to_dict()
    val_path = GTJA_F0_REF / "artifacts" / "validation.json"
    p = None
    if val_path.exists():
        p = (
            json.loads(val_path.read_text(encoding="utf-8"))
            .get("monte_carlo", {})
            .get("p_value_sharpe")
        )
    return {
        "excess_return": float(m.get("excess_return", 0.0)),
        "total_return": float(m.get("total_return", 0.0)),
        "sharpe": float(m.get("sharpe", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "p_sharpe": p,
    }


def _name_map() -> dict[str, str]:
    csv_path = SRC_RUN / "universe.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        if "ts_code" in df.columns and "name" in df.columns:
            return {str(r.ts_code): str(r.name) for r in df.itertuples()}
    meta_path = SRC_RUN / "universe_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (meta.get("names") or {}).items()}
    return {}


def _write_reports(
    df: pd.DataFrame,
    verified: list[dict],
    f1_result: dict | None,
    name_map: dict[str, str],
    *,
    rank_source: str = "fast",
) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_ROOT / "confirm_search_rank.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    f0_runs = [v for v in verified if not v.get("veto_max")]
    best_f0 = max(f0_runs, key=lambda x: float(x["metrics"]["excess_return"]))
    pure = next((v for v in f0_runs if v["label"] == "pure_std30"), None)
    naive = next((v for v in f0_runs if v["label"] == "cord60+klen"), None)

    use_f1 = False
    d_ex_f1 = d_sh_f1 = 0.0
    if f1_result is not None:
        d_ex_f1 = float(f1_result["metrics"]["excess_return"]) - float(
            best_f0["metrics"]["excess_return"]
        )
        d_sh_f1 = float(f1_result["metrics"]["sharpe"]) - float(
            best_f0["metrics"]["sharpe"]
        )
        # Same rule as qlib158 copper / 存储芯片 F0' 185+070:
        # tiny excess bumps that also lose Sharpe are not an increment.
        use_f1 = d_ex_f1 > 0.01 and d_sh_f1 >= 0.0
    winner = f1_result if use_f1 else best_f0
    wm = winner["metrics"]

    hold_md, hold_stats = _holdings_md(Path(winner["run_dir"]), name_map)
    gtja = _gtja_ref_metrics()

    top = df.head(20)
    lines = [
        f"# {PANEL} — Qlib158 存活因子确认腿搜索",
        "",
        "**声明：研究/回测，非实盘建议。**",
        "",
        "## 设定",
        "",
        f"- 过程对齐 GTJA `F0'最优确认-存储芯片-185+070`（周频 / C≥40% / N=6 / 换手帽20% / MA20 择时）",
        f"- 动量主排序固定：`{MOM_ID}`（动量主题 IR 最高，对应 gtja191_164）",
        f"- 禁止确认：反转主题 `{sorted(BANNED_CONFIRM)}`",
        "- IR 权重来自 CSI300 存活库（非存储池重估）",
        f"- 筛选组合数：{len(df)}（排名来源：{rank_source}）；对照 = 纯动量 `pure_std30` 与 IR 朴素对 `cord60+klen`",
        "",
        f"## 筛选 Top20（{rank_source}，按 excess_return）",
        "",
        "| rank | confirm | n | excess | total | sharpe | max_dd | tag |",
        "|-----:|---------|--:|-------:|------:|-------:|-------:|-----|",
    ]
    for i, row in enumerate(top.itertuples(index=False), 1):
        tag = "pure" if row.is_pure else ("naive" if row.is_naive else "")
        lines.append(
            f"| {i} | `{row.label}` | {row.n_confirm} | {_pct(row.excess_return)} | "
            f"{_pct(row.total_return)} | {_num(row.sharpe)} | {_pct(row.max_drawdown)} | "
            f"{tag} |"
        )

    lines += [
        "",
        "## 完整引擎复核（含 MC）",
        "",
        "| label | kind | excess | total | sharpe | max_dd | MC p_sharpe | Δexcess vs 最优F0 |",
        "|-------|------|-------:|------:|-------:|-------:|------------:|------------------:|",
    ]
    base_ex = float(best_f0["metrics"]["excess_return"])
    for v in verified:
        m = v["metrics"]
        kind = "F1+MAX" if v.get("veto_max") else "F0"
        lines.append(
            f"| `{v['label']}` | {kind} | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_mc_p(v)} | "
            f"{_pct(float(m['excess_return']) - base_ex)} |"
        )
    if f1_result is not None:
        m = f1_result["metrics"]
        lines.append(
            f"| `{f1_result['label']}` | F1+MAX | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_mc_p(f1_result)} | "
            f"{_pct(d_ex_f1)} |"
        )

    d_ex_pure = (
        float(best_f0["metrics"]["excess_return"])
        - float(pure["metrics"]["excess_return"])
        if pure
        else 0.0
    )
    lines += [
        "",
        "## 结论",
        "",
        f"- 完整引擎最优 F0 确认腿：`{best_f0['label']}`",
        f"- F0 excess={_pct(best_f0['metrics']['excess_return'])}，"
        f"sharpe={_num(best_f0['metrics']['sharpe'])}，MC p={_mc_p(best_f0)}",
    ]
    if pure:
        lines.append(
            f"- 纯动量对照 excess={_pct(pure['metrics']['excess_return'])}，"
            f"Δexcess={_pct(d_ex_pure)}"
        )
    if naive:
        lines.append(
            f"- IR 朴素对 `cord60+klen` excess={_pct(naive['metrics']['excess_return'])}"
        )
    if f1_result is not None:
        lines.append(
            f"- F1 MAX 否决 Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)} → "
            + ("采用 F1'" if use_f1 else "回退 F0'（同池 F1' 未优于 F0' 时禁止叠加 custom 否决）")
        )
    if gtja is not None:
        lines.append(
            f"- 同池交叉对照 GTJA F0' `185+070` excess={_pct(gtja['excess_return'])}，"
            f"sharpe={_num(gtja['sharpe'])}，MC p={_num(gtja['p_sharpe'])}"
            f"（不同因子库，不可直接当搜索基线）"
        )
    lines += [
        f"- 最近持仓日 {hold_stats['last_date']}；n={hold_stats['n_held']}",
        "",
        hold_md,
        "",
        "- 存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动；确认腿样本内重搜有过拟合风险；需样本外复核。",
        "",
        f"明细 CSV：`{csv_path}`",
        "",
    ]
    REPORT_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ids = list(winner["confirm_ids"])
    ir_weighted = ", ".join(
        f"{aid.replace('qlib158_', '')}:{float(winner.get('irs', {}).get(aid) or 0.0):.3f}"
        for aid in ids
    )
    copy_ids = ", ".join([MOM_ID] + ids + (["custom_max_inv"] if use_f1 else []))
    id_rows = "\n".join(
        [f"| {MOM_ID} | 动量（主排序，必须） |"]
        + [f"| {aid} | 确认（本板块重搜硬确认） |" for aid in ids]
        + (
            ["| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |"]
            if use_f1
            else []
        )
    )
    n_core = 1 + len(ids) + (1 if use_f1 else 0)
    conf_txt = "+".join(a.replace("qlib158_", "") for a in ids) or "无"
    kind = "F1'" if use_f1 else "F0'"
    f1_clause = (
        f"同池 F0'(`{best_f0['label']}`) vs F1'：Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}。"
        + ("故采用 F1'。" if use_f1 else "同池 F1' 未优于 F0' 时禁止叠加 custom 否决。")
    )
    ban = (
        "【禁止】qlib158 反转因子（cntn20/30/60、cntd60）；禁止吸筹/质量确认进硬过滤或重排"
        if use_f1
        else "【禁止】qlib158 反转因子（cntn20/30/60、cntd60）；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排"
    )
    extra_filter = ""
    if use_f1:
        extra_filter = (
            "\n5. F1' 额外否决（仅新开仓）：smooth(custom_max_inv) < 当日截面 20% 分位 → 禁止新开"
        )
    zoo_line = (
        "请用 Qlib 158（qlib158）+ custom_max_inv 做多因子选股并回测"
        if use_f1
        else "请用 Qlib 158（qlib158）做多因子选股并回测"
    )
    d_ex_naive = 0.0
    if naive is not None:
        d_ex_naive = float(best_f0["metrics"]["excess_return"]) - float(
            naive["metrics"]["excess_return"]
        )

    must = []
    if float(wm["excess_return"]) < 0:
        must.append(
            f"- excess={_pct(wm['excess_return'])}<0：未跑赢板块等权基准，必须明文承认。"
        )
    else:
        must.append(
            f"- excess={_pct(wm['excess_return'])}>0：本样本相对板块基准为正，仍须样本外复核。"
        )
    p = ((winner.get("validation") or {}).get("monte_carlo") or {}).get("p_value_sharpe")
    if p is not None and float(p) > 0.2:
        must.append(f"- MC p_sharpe={_num(p)}>0.2。")
    else:
        must.append(f"- MC p_sharpe={_num(p)}（阈值 0.2）。")
    must.append(
        f"- 相对纯动量 std30：Δexcess={_pct(d_ex_pure)}；确认腿为板块内存活因子重搜结果，有过拟合风险。"
    )
    must_block = "\n".join(must)

    body = f"""## 提示词

```
{zoo_line}，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为存储芯片板块（{UNIVERSE_DESC}）上，在 Qlib158 存活因子中自由组合重搜确认腿后的最优复现：
{kind} = 动量 std30 + 硬确认 {conf_txt}{" + custom_max_inv 底部否决" if use_f1 else ""}。
对照纯动量 std30：完整引擎 Δexcess={_pct(d_ex_pure)}。
{f1_clause}
对照 IR 朴素对 cord60/klen：Δexcess={_pct(d_ex_naive)}。
禁止为追收益叠加未授权否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{must_block}

【股票池与区间】
- 标的池：A股存储芯片相关板块（{UNIVERSE_DESC}）
- 研究/回测参考区间：{START_DATE} 至 {END_DATE}（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 {n_core} 个）】
| ID | 角色 |
{id_rows}

复制用：{copy_ids}
{ban}
【禁止】回退使用 IR 朴素对 cord60/klen 作为本版确认腿（除非其完整引擎仍最优）

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(qlib158_std30)（纯动量）
3. 确认分 C = IR 加权({ir_weighted or "无确认腿"})；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 面板只建 OHLCV{extra_filter}

【持仓与换手（硬性）】
1. N=6 等权；保留带 Top9（N×1.5）
2. 调仓日：过确认后按 S 选 Top6；旧仓仍在 Top9 且未触发风控可续持
3. 换手硬顶 20%：优先留旧仓，再填新股；将超 20% 则停止换入（空仓建仓首期除外）
4. 最小持有 5 日：未满 5 日不因跌出保留带而卖；峰值回撤止损/止盈立即生效
5. 择时：板块等权均价 vs MA20，仅调仓日更新乘数（1.0/0.5）；非调仓日冻结

【退出】
峰值回撤≥15% → 止盈≥35% → 持仓≥20日且排名恶化≥10名 → 调仓日跌出 Top9（且满 5 日）
非调仓日不因排名换股。

【交付】
- 因子 ID + 角色；未使用反转/吸筹；确认腿为本板块存活因子重搜结果
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照：同池纯动量 std30 与 F1'，给出 Δtotal / Δexcess / Δsharpe；说明为何以 {kind} 为准
- 3–5 条白话：存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动；确认腿样本内重搜有过拟合风险
- 声明：研究/回测，非实盘建议

```

## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **{kind.replace("'", "")}** / `{conf_txt}` |
| F0' run | `{Path(best_f0["run_dir"]).name}` |
| F1' run | `{Path(f1_result["run_dir"]).name if f1_result else "n/a"}` |
| excess / sharpe / max_dd | {_pct(wm["excess_return"])} / {_num(wm["sharpe"])} / {_pct(wm["max_drawdown"])} |
| MC p_sharpe | {_mc_p(winner)} |
| Δexcess vs 纯动量 std30 | {_pct(d_ex_pure)} |
| 最近持仓日 | {hold_stats["last_date"]}（n={hold_stats["n_held"]}） |

{hold_md}

研究/回测，非实盘建议。
"""
    PROP_OUT.write_text(body, encoding="utf-8")
    print(f"wrote prop {PROP_OUT}")
    print(f"wrote report {REPORT_OUT}")
    print(f"wrote csv {csv_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pair", type=int, default=2, choices=(1, 2))
    ap.add_argument("--top-k", type=int, default=5, help="MC verify top-K by the ranking used")
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument(
        "--rank-engine",
        action="store_true",
        help="re-rank every combo with the market engine (mc=0) before MC top-K; "
        "needed because the in-memory fast path can invert vs costs/slippage",
    )
    args = ap.parse_args()

    irs = _load_alive_irs()
    if MOM_ID not in irs:
        raise SystemExit(f"{MOM_ID} not alive in survival store")
    print(f"[survival] alive qlib158={sum(1 for k in irs if k.startswith('qlib158_'))}")

    data_map = _load_ohlcv_src(SRC_RUN)
    panel = _build_panel(data_map)
    close = panel["close"]
    print(f"[panel] {PANEL} symbols={close.shape[1]} bars={close.shape[0]}")

    # Precompute mom + confirm candidates + banned (banned stay out of combos).
    alive_ids = [MOM_ID] + [
        aid
        for aid in sorted(irs)
        if aid.startswith("qlib158_") and aid != MOM_ID
    ]
    print("[precompute] factors…")
    scores = _precompute_scores(panel, alive_ids)
    if MOM_ID not in scores:
        raise SystemExit("mom factor failed")

    cands = [
        aid
        for aid in sorted(scores)
        if aid != MOM_ID and aid not in BANNED_CONFIRM
    ]
    print(f"[search] confirm candidates={len(cands)} max_pair={args.max_pair}")

    rows: list[dict] = []
    t0 = time.time()
    combos: list[tuple[str, ...]] = [tuple()]  # pure mom control
    combos.append(NAIVE_CONFIRM)
    if args.max_pair >= 1:
        combos.extend((a,) for a in cands)
    if args.max_pair >= 2:
        for a, b in itertools.combinations(cands, 2):
            if tuple(sorted((a, b))) == tuple(sorted(NAIVE_CONFIRM)):
                continue
            combos.append((a, b))
    print(f"[search] combo count={len(combos)}")
    seen: set[tuple[str, ...]] = set()
    for i, combo in enumerate(combos, 1):
        key = tuple(combo)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_score_combo(combo, scores, irs, close))
        if i % 50 == 0 or i == len(combos):
            print(f"  scored {i}/{len(combos)} elapsed={time.time() - t0:.1f}s")

    df = pd.DataFrame(rows).sort_values(
        ["excess_return", "sharpe", "total_return"], ascending=False
    ).reset_index(drop=True)
    print(
        f"[fast] done n={len(df)} in {time.time() - t0:.1f}s; "
        f"best={df.iloc[0]['label']} excess={df.iloc[0]['excess_return']:.4f}"
    )

    if args.rank_engine:
        # Fast-path PnL ignores stamp/slippage path-dependence; on this 75-name
        # panel that inverted the top-5 vs the market engine. Re-rank every
        # combo with mc=0, keep artifacts only under screen_*, then MC top-K.
        print(f"[engine-rank] scoring {len(df)} combos with market engine (mc=0)…")
        engine_rows: list[dict] = []
        t1 = time.time()
        for i, row in enumerate(df.itertuples(index=False), 1):
            ids = list(row.confirm_ids)
            lab = _combo_label(tuple(ids))
            run_dir = OUT_ROOT / f"screen_{lab}"
            metrics_path = run_dir / "artifacts" / "metrics.csv"
            if metrics_path.exists():
                m = pd.read_csv(metrics_path).iloc[0].to_dict()
            else:
                result = _full_engine_run(ids, scores, irs, data_map, run_dir, mc_sims=0)
                m = result["metrics"]
            engine_rows.append(
                {
                    "label": lab,
                    "confirm_ids": ids,
                    "n_confirm": len(ids),
                    "is_pure": len(ids) == 0,
                    "is_naive": ids == list(NAIVE_CONFIRM),
                    "is_rebalance_days": row.is_rebalance_days,
                    "total_return": float(m.get("total_return", 0.0)),
                    "excess_return": float(m.get("excess_return", 0.0)),
                    "sharpe": float(m.get("sharpe", 0.0)),
                    "max_drawdown": float(m.get("max_drawdown", 0.0)),
                    "benchmark_return": float(m.get("benchmark_return", 0.0)),
                    "rebalance_turnover_mean": float(
                        m.get("rebalance_turnover_mean", 0.0) or 0.0
                    ),
                    "irs": row.irs,
                }
            )
            if i % 20 == 0 or i == len(df):
                print(
                    f"  engine {i}/{len(df)} elapsed={time.time() - t1:.1f}s "
                    f"last={lab} excess={float(m.get('excess_return', 0.0)):.4f}"
                )
        df = pd.DataFrame(engine_rows).sort_values(
            ["excess_return", "sharpe", "total_return"], ascending=False
        ).reset_index(drop=True)
        print(
            f"[engine-rank] done; best={df.iloc[0]['label']} "
            f"excess={df.iloc[0]['excess_return']:.4f}"
        )

    verify_ids: list[list[str]] = []
    verify_labels: list[str] = []

    def _add(ids: list[str]) -> None:
        lab = _combo_label(tuple(ids))
        if lab in verify_labels:
            return
        verify_labels.append(lab)
        verify_ids.append(ids)

    _add([])  # pure mom
    _add(list(NAIVE_CONFIRM))
    for _, row in df.head(max(args.top_k, 1)).iterrows():
        _add(list(row["confirm_ids"]))
    best_single = df.loc[df["n_confirm"] == 1]
    best_pair = df.loc[df["n_confirm"] == 2]
    if len(best_single):
        _add(list(best_single.iloc[0]["confirm_ids"]))
    if len(best_pair):
        _add(list(best_pair.iloc[0]["confirm_ids"]))

    verified: list[dict] = []
    print(f"[full-engine] verifying {len(verify_ids)} combos…")
    for ids in verify_ids:
        lab = _combo_label(tuple(ids))
        run_dir = OUT_ROOT / f"full_{lab}"
        print(f"  → {lab}")
        result = _full_engine_run(ids, scores, irs, data_map, run_dir, args.mc_sims)
        verified.append(result)
        m = result["metrics"]
        print(
            f"    excess={m.get('excess_return')} sharpe={m.get('sharpe')} "
            f"mc_p={(result.get('validation') or {}).get('monte_carlo', {}).get('p_value_sharpe')}"
        )

    verified.sort(key=lambda x: float(x["metrics"]["excess_return"]), reverse=True)
    best_f0 = verified[0]
    print(f"[stage4] F1 MAX veto on best confirm {best_f0['label']}…")
    f1_dir = OUT_ROOT / f"full_{best_f0['label']}_F1"
    f1_result = _full_engine_run(
        list(best_f0["confirm_ids"]),
        scores,
        irs,
        data_map,
        f1_dir,
        args.mc_sims,
        veto_max=True,
    )
    print(
        f"  F1 excess={f1_result['metrics'].get('excess_return')} "
        f"sharpe={f1_result['metrics'].get('sharpe')} mc_p={_mc_p(f1_result)}"
    )

    _write_reports(
        df,
        verified,
        f1_result,
        _name_map(),
        rank_source="market-engine" if args.rank_engine else "fast",
    )
    print(
        f"[done] full-engine best F0 confirm={best_f0['label']} "
        f"excess={best_f0['metrics'].get('excess_return')} "
        f"sharpe={best_f0['metrics'].get('sharpe')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
