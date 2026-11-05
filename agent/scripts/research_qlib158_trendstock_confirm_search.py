"""Search qlib158 confirm legs on 趋势股 BK1715 (market-pick rank #2).

Same family as the 金属铜 / 存储芯片 F0' runs: momentum sort is fixed at
``qlib158_std30`` (highest-IR momentum survivor), confirm legs are singles and
pairs among qlib158 survivors, reversal names stay banned, portfolio process is
weekly-first-day / C>=40% IR-weighted / N=6 / band Top9 / 20% turnover / MA20.

Differences from the 存储芯片 script:

* candidates = survival-store / fallback IR set **plus** the top BK1715-pool
  IC factors (sign-oriented on this panel), so the search is driven by what
  actually works on a 趋势股 board rather than CSI300 alone;
* the copper F0' (``kmid2+max60``) and 存储芯片 F0' (``kmid+ksft2``) legs are
  forced into the candidate set, so both reference sectors get an apples-to-
  apples verdict on this pool;
* the winning combo is also mirrored into a clean ``runs/<id>`` directory with
  req.json + run_card.json so the Reports page lists it as a backtest record.

Run from ``agent/``:
    python scripts/research_qlib158_trendstock_confirm_search.py --top-k 8
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
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
from scripts.research_sector_confirm_search import (  # noqa: E402
    _holdings_md,
    _load_ohlcv as _load_ohlcv_src,
)
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402

SRC_RUN = _AGENT_ROOT / "runs" / "20261103_201500_00_trendstock_universe"
OUT_ROOT = _AGENT_ROOT / "runs" / "_research_qlib158_trendstock_confirm_search"
PROP_OUT = _AGENT_ROOT / "prop_qlib158_趋势股_最优.md"
REPORT_OUT = OUT_ROOT / "confirm_search_summary.md"

PANEL = "趋势股 BK1715"
UNIVERSE_DESC = "东财概念「趋势股」BK1715（市场扫描排名第2；排除北交所；成分80只）"
START_DATE = "2024-01-01"
END_DATE = "2026-11-03"
INITIAL_CASH = 1_000_000

MOM_ID = "qlib158_std30"
BANNED_CONFIRM = frozenset(
    {
        "qlib158_cntn20",
        "qlib158_cntn30",
        "qlib158_cntn60",
        "qlib158_cntd60",
    }
)
NAIVE_CONFIRM = ("qlib158_cord60", "qlib158_klen")
# Reference legs from the two published sector winners.
REFERENCE_COMBOS: dict[str, tuple[str, ...]] = {
    "copper_kmid2+max60": ("qlib158_kmid2", "qlib158_max60"),
    "memory_kmid+ksft2": ("qlib158_kmid", "qlib158_ksft2"),
}

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

POOL_TOP_N = 10
POOL_MIN_ABS_IC = 0.02


def _load_irs() -> dict[str, float]:
    """Survival-store alive IRs, topped up by the CSI300 repair table."""
    out: dict[str, float] = {}
    path = survival_path()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            alphas = payload.get("alphas") or payload
            for aid, row in alphas.items():
                if not str(aid).startswith("qlib158_") or not isinstance(row, dict):
                    continue
                if row.get("category") != "alive":
                    continue
                out[str(aid)] = float(row.get("ir") or FALLBACK_IR.get(aid, 0.10))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            out = {}
    for aid, ir in FALLBACK_IR.items():
        out.setdefault(aid, ir)
    return out


def _build_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    cols = sorted(data_map)
    return {
        "close": pd.DataFrame({c: data_map[c]["close"] for c in cols}),
        "open": pd.DataFrame({c: data_map[c]["open"] for c in cols}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in cols}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in cols}),
        "volume": pd.DataFrame({c: data_map[c]["volume"] for c in cols}),
    }


def _forward_returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.pct_change().shift(-1)


def _pool_ic(raw: pd.DataFrame, fwd: pd.DataFrame) -> dict[str, float]:
    """Daily cross-sectional rank IC stats for one factor on this pool."""
    idx = raw.index.intersection(fwd.index)
    cols = raw.columns.intersection(fwd.columns)
    if len(idx) < 60 or len(cols) < 5:
        return {"ic_mean": 0.0, "ic_std": 0.0, "ir": 0.0, "pos_ratio": 0.0, "n": 0}
    fr = raw.loc[idx, cols].rank(axis=1)
    rr = fwd.loc[idx, cols].rank(axis=1)
    ic = fr.corrwith(rr, axis=1).dropna()
    if ic.empty:
        return {"ic_mean": 0.0, "ic_std": 0.0, "ir": 0.0, "pos_ratio": 0.0, "n": 0}
    mean = float(ic.mean())
    std = float(ic.std())
    return {
        "ic_mean": mean,
        "ic_std": std,
        "ir": mean / std if std > 0 else 0.0,
        "pos_ratio": float((ic > 0).mean()),
        "n": int(len(ic)),
    }


def _compute_all(
    panel: dict[str, pd.DataFrame], ids: list[str], fwd: pd.DataFrame
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Compute every candidate factor once; return oriented+smoothed scores and IC table."""
    reg = Registry()
    close = panel["close"]
    scores: dict[str, pd.DataFrame] = {}
    rows: list[dict] = []
    for aid in ids:
        t0 = time.time()
        try:
            raw = reg.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {aid}: {exc}")
            continue
        if raw is None or raw.empty:
            print(f"  [skip] {aid}: empty")
            continue
        raw = raw.reindex(index=close.index, columns=close.columns)
        stats = _pool_ic(raw, fwd)
        # Orient on the pool so "higher percentile = more bullish" holds.
        sign = -1.0 if stats["ic_mean"] < 0 else 1.0
        oriented = raw * sign if sign < 0 else raw
        scores[aid] = _smooth_cs(oriented)
        rows.append({"id": aid, "sign": sign, **stats})
        print(
            f"  factor {aid}: ic={stats['ic_mean']:+.4f} ir={stats['ir']:+.3f} "
            f"pos={stats['pos_ratio']:.2f} sign={sign:+.0f} ({time.time() - t0:.2f}s)"
        )
    return scores, pd.DataFrame(rows)


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
    """Full-engine adapter over precomputed smooth scores; optional MAX veto."""

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
        if self.veto_max and "custom_max_inv" in self.scores:
            mx = self.scores["custom_max_inv"].reindex(
                index=close.index, columns=close.columns
            )
            thr = mx.quantile(0.20, axis=1)
            ok = ok & mx.ge(thr, axis=0).fillna(True)
        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _ensure_max_inv(scores: dict[str, pd.DataFrame], panel: dict[str, pd.DataFrame]) -> None:
    if "custom_max_inv" in scores:
        return
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
    tag: str = "",
) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    if veto_max:
        _ensure_max_inv(scores, _build_panel(data_map))

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
            "tag": tag,
        },
        "_run_card_effective_sources": ["local_artifacts"],
    }
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
        v = float(x)
        if math.isnan(v):
            return "n/a"
        return f"{v * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x, nd: int = 4) -> str:
    try:
        v = float(x)
        if math.isnan(v):
            return "n/a"
        return f"{v:.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _mc_p(v: dict) -> str:
    mc = (v.get("validation") or {}).get("monte_carlo") or {}
    return _num(mc.get("p_value_sharpe"))


def _name_map() -> dict[str, str]:
    meta_path = SRC_RUN / "universe_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in (meta.get("names") or {}).items() if v}
    return {}


def _write_reports(
    df: pd.DataFrame,
    ic_table: pd.DataFrame,
    verified: list[dict],
    f1_result: dict | None,
    name_map: dict[str, str],
    candidates: list[str],
) -> dict:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_ROOT / "confirm_search_rank.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    ic_table.sort_values("ir", ascending=False).to_csv(
        OUT_ROOT / "pool_ic.csv", index=False, encoding="utf-8-sig"
    )

    f0_runs = [v for v in verified if not v.get("veto_max")]
    best_f0 = max(f0_runs, key=lambda x: float(x["metrics"]["excess_return"]))
    pure = next((v for v in f0_runs if v["label"] == "pure_std30"), None)
    naive = next((v for v in f0_runs if v["label"] == _combo_label(NAIVE_CONFIRM)), None)
    refs = {
        key: next((v for v in f0_runs if v["label"] == _combo_label(legs)), None)
        for key, legs in REFERENCE_COMBOS.items()
    }

    use_f1 = False
    d_ex_f1 = d_sh_f1 = 0.0
    if f1_result is not None:
        d_ex_f1 = float(f1_result["metrics"]["excess_return"]) - float(
            best_f0["metrics"]["excess_return"]
        )
        d_sh_f1 = float(f1_result["metrics"]["sharpe"]) - float(best_f0["metrics"]["sharpe"])
        use_f1 = d_ex_f1 > 0.01 and d_sh_f1 >= 0.0
    winner = f1_result if use_f1 else best_f0
    wm = winner["metrics"]
    hold_md, hold_stats = _holdings_md(Path(winner["run_dir"]), name_map)

    d_ex_pure = (
        float(best_f0["metrics"]["excess_return"]) - float(pure["metrics"]["excess_return"])
        if pure
        else 0.0
    )
    d_sh_pure = (
        float(best_f0["metrics"]["sharpe"]) - float(pure["metrics"]["sharpe"]) if pure else 0.0
    )
    d_ex_naive = (
        float(best_f0["metrics"]["excess_return"]) - float(naive["metrics"]["excess_return"])
        if naive
        else 0.0
    )

    top_ic = ic_table.sort_values("ir", ascending=False).head(12)
    lines = [
        f"# {PANEL} — Qlib158 存活因子确认腿搜索",
        "",
        "**声明：研究/回测，非实盘建议。**",
        "",
        "## 设定",
        "",
        f"- 标的池：{UNIVERSE_DESC}，{len(candidates)} 个候选确认腿，区间 {START_DATE} ~ {END_DATE}",
        "- 过程对齐 金属铜 / 存储芯片 的 F0'：周频首个交易日调仓 / IR 加权 C≥40% / N=6 / 保留带 Top9 / 换手帽 20% / MA20 择时",
        f"- 动量主排序固定 `{MOM_ID}`（动量主题 IR 最高，对应 gtja191_164）",
        f"- 禁止确认：反转主题 `{sorted(BANNED_CONFIRM)}`",
        "- 对照：纯动量 `pure_std30`、IR 朴素对 `cord60+klen`、金属铜 `kmid2+max60`、存储芯片 `kmid+ksft2`",
        "",
        "## 本池有效因子（BK1715 截面 IC，按 IR 排序）",
        "",
        "| # | factor | IC mean | IR | IC>0 占比 | 方向 |",
        "|--:|--------|--------:|---:|----------:|-----:|",
    ]
    for i, row in enumerate(top_ic.itertuples(index=False), 1):
        lines.append(
            f"| {i} | `{row.id.replace('qlib158_', '')}` | {row.ic_mean:+.4f} | "
            f"{row.ir:+.3f} | {row.pos_ratio:.2f} | {'正向' if row.sign > 0 else '反向(已翻转)'} |"
        )

    lines += [
        "",
        f"## 快筛 Top20（按 excess_return，共 {len(df)} 组合）",
        "",
        "| rank | confirm | n | excess | total | sharpe | max_dd | tag |",
        "|-----:|---------|--:|-------:|------:|-------:|-------:|-----|",
    ]
    for i, row in enumerate(df.head(20).itertuples(index=False), 1):
        tag = "pure" if row.is_pure else ("naive" if row.is_naive else "")
        lines.append(
            f"| {i} | `{row.label}` | {row.n_confirm} | {_pct(row.excess_return)} | "
            f"{_pct(row.total_return)} | {_num(row.sharpe)} | {_pct(row.max_drawdown)} | {tag} |"
        )

    lines += [
        "",
        "## 完整引擎复核（含蒙特卡洛）",
        "",
        "| label | 来源 | excess | total | sharpe | max_dd | MC p_sharpe |",
        "|-------|------|-------:|------:|-------:|-------:|------------:|",
    ]
    for v in verified:
        m = v["metrics"]
        src = "对照-纯动量" if v["label"] == "pure_std30" else (
            "对照-IR朴素" if v["label"] == _combo_label(NAIVE_CONFIRM) else "候选"
        )
        if v["label"] == _combo_label(REFERENCE_COMBOS["copper_kmid2+max60"]):
            src = "对照-金属铜"
        if v["label"] == _combo_label(REFERENCE_COMBOS["memory_kmid+ksft2"]):
            src = "对照-存储芯片"
        lines.append(
            f"| `{v['label']}` | {src} | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_mc_p(v)} |"
        )
    if f1_result is not None:
        m = f1_result["metrics"]
        lines.append(
            f"| `{f1_result['label']}` | F1+MAX否决 | {_pct(m['excess_return'])} | "
            f"{_pct(m['total_return'])} | {_num(m['sharpe'])} | "
            f"{_pct(m['max_drawdown'])} | {_mc_p(f1_result)} |"
        )

    lines += [
        "",
        "## 结论",
        "",
        f"- 最优确认腿：`{best_f0['label']}`（excess={_pct(best_f0['metrics']['excess_return'])}，"
        f"sharpe={_num(best_f0['metrics']['sharpe'])}，MC p={_mc_p(best_f0)}）",
        f"- 对照纯动量 std30：Δexcess={_pct(d_ex_pure)}，Δsharpe={_num(d_sh_pure)}",
    ]
    if naive:
        lines.append(
            f"- 对照 IR 朴素对 `cord60+klen`：excess={_pct(naive['metrics']['excess_return'])}，"
            f"Δexcess={_pct(d_ex_naive)}"
        )
    for key, v in refs.items():
        if v is None:
            continue
        lines.append(
            f"- 对照组 {key}：excess={_pct(v['metrics']['excess_return'])}，"
            f"sharpe={_num(v['metrics']['sharpe'])}"
        )
    if f1_result is not None:
        lines.append(
            f"- F1 MAX 否决 Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)} → "
            + ("采用 F1'" if use_f1 else "回退 F0'")
        )
    lines += [
        f"- 最近持仓日 {hold_stats['last_date']}；n={hold_stats['n_held']}",
        "",
        hold_md,
        "",
        "- 趋势股是概念板块，成分随行情换手，回测用的是 2026-11-03 的静态成分，存在前视/幸存者偏差。",
        "- 确认腿在本池样本内重搜，过拟合风险高于跨板块通用因子；需样本外复核。",
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
    conf_txt = "+".join(a.replace("qlib158_", "") for a in ids) or "无"
    kind = "F1'" if use_f1 else "F0'"
    d_ex_naive = 0.0 if naive is None else (
        float(best_f0["metrics"]["excess_return"]) - float(naive["metrics"]["excess_return"])
    )
    must = []
    if float(wm["excess_return"]) < 0:
        must.append(f"- excess={_pct(wm['excess_return'])}<0：未跑赢板块等权基准，必须明文承认。")
    else:
        must.append(
            f"- excess={_pct(wm['excess_return'])}>0：本样本相对板块基准为正，仍须样本外复核。"
        )
    p = ((winner.get("validation") or {}).get("monte_carlo") or {}).get("p_value_sharpe")
    must.append(f"- MC p_sharpe={_num(p)}（阈值 0.2）。")
    must.append(
        f"- 相对纯动量 std30：Δexcess={_pct(d_ex_pure)}，Δsharpe={_num(d_sh_pure)}；确认腿为本板块重搜结果，有过拟合风险。"
    )

    prop = f"""## 提示词

```
请用 Qlib 158（qlib158）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为趋势股板块（{UNIVERSE_DESC}）上，在 Qlib158 存活因子中自由组合重搜确认腿后的最优复现：
{kind} = 动量 std30 + 硬确认 {conf_txt}。
对照纯动量 std30：完整引擎 Δexcess={_pct(d_ex_pure)}，Δsharpe={_num(d_sh_pure)}。
同池实测 金属铜 `kmid2+max60` 与 存储芯片 `kmid+ksft2` 均未优于本版。
{f"同池 F0'(`{best_f0['label']}`) vs F1'：Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}。" + ("故采用 F1'。" if use_f1 else "同池 F1' 未优于 F0' 时禁止叠加 custom 否决。")}
对照 IR 朴素对 cord60/klen：Δexcess={_pct(d_ex_naive)}。
禁止为追收益叠加未授权否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{chr(10).join(must)}

【股票池与区间】
- 标的池：{UNIVERSE_DESC}
- 研究/回测参考区间：{START_DATE} 至 {END_DATE}
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 {1 + len(ids) + (1 if use_f1 else 0)} 个）】
| ID | 角色 |
|----|------|
| {MOM_ID} | 动量（主排序，必须） |
{chr(10).join(f"| {aid} | 确认（本板块重搜硬确认） |" for aid in ids)}
{f"| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |" if use_f1 else ""}
复制用：{", ".join([MOM_ID] + ids + (["custom_max_inv"] if use_f1 else []))}
【禁止】qlib158 反转因子（cntn20/30/60、cntd60）；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(qlib158_std30)（纯动量）
3. 确认分 C = IR 加权({ir_weighted})；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 面板只建 OHLCV

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
- 必须对照：同池纯动量 std30、金属铜 / 存储芯片 参考组合，给出 Δtotal / Δexcess / Δsharpe
- 3–5 条白话：概念板块成分换手与前视偏差、动量拥挤与追高风险、确认腿样本内过拟合风险
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
    PROP_OUT.write_text(prop, encoding="utf-8")
    print(f"wrote prop {PROP_OUT}")
    print(f"wrote report {REPORT_OUT}")
    print(f"wrote csv {csv_path}")
    return {"winner": winner, "best_f0": best_f0, "pure": pure, "use_f1": use_f1,
            "d_ex_pure": d_ex_pure, "d_sh_pure": d_sh_pure, "hold_stats": hold_stats}


def _publish_run(winner: dict, name_map: dict[str, str], *, use_f1: bool) -> Path:
    """Mirror the winning run into a clean, self-describing run directory."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    legs = "_".join(a.replace("qlib158_", "") for a in winner["confirm_ids"]) or "pure"
    run_id = f"{stamp}_05_F0_trendstock_{legs}" + ("_F1" if use_f1 else "")
    dest = _AGENT_ROOT / "runs" / run_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(Path(winner["run_dir"]), dest)

    title = f"趋势股BK1715 F0' {legs} — Qlib158最优确认腿"
    prompt = (
        f"市场扫描第2板块 趋势股 BK1715 成分股回测：qlib158_std30 动量 + 硬确认 {legs}，"
        f"参考金属铜/存储芯片 F0' 过程，选 excess_return 最优组合。"
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
        json.dumps({"status": "success", "source": "research_qlib158_trendstock"}, ensure_ascii=False),
        encoding="utf-8",
    )

    # Human-readable report + run card markdown, mirroring the sector 最优 runs.
    m = winner["metrics"]
    hold_md, hold_stats = _holdings_md(dest, name_map)
    body = f"""# {title}

**研究/回测，非实盘建议。**

## 设定

- 标的池：{UNIVERSE_DESC}
- 区间：{START_DATE} ~ {END_DATE}，source=lake（本地日线），周频首个交易日调仓
- 因子：`{MOM_ID}` 动量 + 确认 `{", ".join(a.replace("qlib158_", "") for a in winner["confirm_ids"])}`
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

明细见 `../_research_qlib158_trendstock_confirm_search/confirm_search_summary.md` 与 `prop_qlib158_趋势股_最优.md`。
"""
    (dest / f"REPORT_F0_trendstock_{legs}.md").write_text(body, encoding="utf-8")
    print(f"[publish] {dest}")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pair", type=int, default=2, choices=(1, 2))
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument("--pool-top", type=int, default=POOL_TOP_N)
    args = ap.parse_args()

    irs = _load_irs()
    if MOM_ID not in irs:
        raise SystemExit(f"{MOM_ID} not alive in survival/fallback IR table")
    print(f"[ir] candidates from survival+fallback={len(irs)}")

    data_map = _load_ohlcv_src(SRC_RUN)
    panel = _build_panel(data_map)
    close = panel["close"]
    fwd = _forward_returns(close)
    print(f"[panel] {PANEL} symbols={close.shape[1]} bars={close.shape[0]}")

    reg_ids = [a for a in Registry().list(zoo="qlib158")]
    print(f"[pool-ic] computing {len(reg_ids)} qlib158 factors…")
    all_scores, ic_table = _compute_all(panel, reg_ids, fwd)
    if MOM_ID not in all_scores:
        raise SystemExit("momentum factor failed on this pool")

    pool_top = (
        ic_table[(ic_table["ic_mean"].abs() >= POOL_MIN_ABS_IC) & (ic_table["n"] >= 120)]
        .sort_values("ir", ascending=False)
        .head(args.pool_top)["id"]
        .tolist()
    )
    base_cands = [a for a in sorted(irs) if a != MOM_ID and a in all_scores]
    extra = [a for a in pool_top if a not in base_cands and a != MOM_ID]
    ref_legs = [a for legs in REFERENCE_COMBOS.values() for a in legs if a in all_scores]
    cands = [a for a in dict.fromkeys(base_cands + extra + ref_legs) if a not in BANNED_CONFIRM]
    print(f"[search] pool-top added={extra}")
    print(f"[search] confirm candidates={len(cands)}")

    rows: list[dict] = []
    t0 = time.time()
    combos: list[tuple[str, ...]] = [tuple(), NAIVE_CONFIRM]
    combos.extend(REFERENCE_COMBOS.values())
    if args.max_pair >= 1:
        combos.extend((a,) for a in cands)
    if args.max_pair >= 2:
        for a, b in itertools.combinations(cands, 2):
            combos.append((a, b))
    print(f"[search] combo count={len(combos)}")
    seen: set[tuple[str, ...]] = set()
    for i, combo in enumerate(combos, 1):
        key = tuple(sorted(combo))
        if key in seen:
            continue
        seen.add(key)
        rows.append(_score_combo(combo, {k: all_scores[k] for k in [MOM_ID, *combo]}, irs, close))
        if i % 50 == 0 or i == len(combos):
            print(f"  scored {i}/{len(combos)} elapsed={time.time() - t0:.1f}s")

    df = pd.DataFrame(rows).sort_values(
        ["excess_return", "sharpe", "total_return"], ascending=False
    ).reset_index(drop=True)
    print(
        f"[fast] done n={len(df)} in {time.time() - t0:.1f}s; "
        f"best={df.iloc[0]['label']} excess={df.iloc[0]['excess_return']:.4f}"
    )

    verify: list[list[str]] = []
    seen_labels: set[str] = set()

    def _add(ids: list[str]) -> None:
        lab = _combo_label(tuple(ids))
        if lab in seen_labels:
            return
        seen_labels.add(lab)
        verify.append(ids)

    _add([])
    _add(list(NAIVE_CONFIRM))
    for legs in REFERENCE_COMBOS.values():
        _add(list(legs))
    for _, row in df.head(max(args.top_k, 1)).iterrows():
        _add(list(row["confirm_ids"]))
    for n in (1, 2):
        sub = df[df["n_confirm"] == n]
        if len(sub):
            _add(list(sub.iloc[0]["confirm_ids"]))

    verified: list[dict] = []
    print(f"[full-engine] verifying {len(verify)} combos…")
    for ids in verify:
        lab = _combo_label(tuple(ids))
        run_dir = OUT_ROOT / f"full_{lab}"
        print(f"  → {lab}")
        result = _full_engine_run(ids, all_scores, irs, data_map, run_dir, args.mc_sims)
        verified.append(result)
        m = result["metrics"]
        print(
            f"    excess={_pct(m.get('excess_return'))} sharpe={_num(m.get('sharpe'))} "
            f"mc_p={_mc_p(result)}"
        )

    f0_verified = [v for v in verified if not v.get("veto_max")]
    f0_verified.sort(key=lambda x: float(x["metrics"]["excess_return"]), reverse=True)
    best_f0 = f0_verified[0]
    print(f"[stage4] F1 MAX veto on best confirm {best_f0['label']}…")
    f1_result = _full_engine_run(
        list(best_f0["confirm_ids"]),
        all_scores,
        irs,
        data_map,
        OUT_ROOT / f"full_{best_f0['label']}_F1",
        args.mc_sims,
        veto_max=True,
    )
    print(
        f"  F1 excess={_pct(f1_result['metrics'].get('excess_return'))} "
        f"sharpe={_num(f1_result['metrics'].get('sharpe'))} mc_p={_mc_p(f1_result)}"
    )

    summary = _write_reports(df, ic_table, verified, f1_result, _name_map(), cands)
    dest = _publish_run(summary["winner"], _name_map(), use_f1=summary["use_f1"])
    print(
        f"[done] best F0 confirm={best_f0['label']} "
        f"excess={_pct(best_f0['metrics'].get('excess_return'))} "
        f"sharpe={_num(best_f0['metrics'].get('sharpe'))} published={dest.name}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
