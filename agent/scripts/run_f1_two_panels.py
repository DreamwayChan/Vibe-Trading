"""Run F1 (MAX bottom-20% veto) on robot + innovate-drug cached OHLCV; write 2 reports."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import (  # noqa: E402
    _AutoLoader,
    _create_market_engine,
    _load_module_from_file,
)

ENGINE_SRC = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "veto_max_20" / "code" / "signal_engine.py"
)
F0_ENGINE_SRC = (
    _AGENT_ROOT / "runs" / "20260725_182200_02_F0_dual" / "code" / "signal_engine.py"
)

PANELS = [
    {
        "key": "robot",
        "label": "人形机器人 BK1184",
        "src": _AGENT_ROOT / "runs" / "20260725_094658_01_531d4f",
        "f1_run": _AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
        "f0_run": _AGENT_ROOT / "runs" / "20260725_184000_01_F0_robot",
        "baseline_metrics_hint": "C/F0 published on same OHLCV",
    },
    {
        "key": "drug",
        "label": "创新药",
        "src": _AGENT_ROOT / "runs" / "20260725_144646_23_598010",
        "f1_run": _AGENT_ROOT / "runs" / "20260725_184000_02_F1_drug",
        "f0_run": _AGENT_ROOT / "runs" / "20260725_184000_02_F0_drug",
        "baseline_metrics_hint": "source run metrics.csv (prior strategy on same pool)",
    },
]


def _load_ohlcv(art: Path, codes: list[str]) -> dict[str, pd.DataFrame]:
    data_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if not path.exists():
            # try without exchange suffix variants already in filename
            continue
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        data_map[code] = df[keep].copy()
    if not data_map:
        # fallback: load all ohlcv_*.csv present
        for path in sorted(art.glob("ohlcv_*.csv")):
            code = path.stem.replace("ohlcv_", "", 1)
            df = pd.read_csv(path)
            date_col = "trade_date" if "trade_date" in df.columns else "date"
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.set_index(date_col).sort_index()
            keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
            data_map[code] = df[keep].copy()
    return data_map


def _prepare(dst: Path, src: Path, engine_src: Path, strategy: str) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    (dst / "artifacts").mkdir(parents=True)
    for p in (src / "artifacts").glob("ohlcv_*.csv"):
        shutil.copy2(p, dst / "artifacts" / p.name)
    shutil.copy2(engine_src, dst / "code" / "signal_engine.py")
    cfg = json.loads((src / "config.json").read_text(encoding="utf-8"))
    if "initial_cash" not in cfg and "initial_capital" in cfg:
        cfg["initial_cash"] = cfg["initial_capital"]
    # Keep source costs; tag strategy
    params = dict(cfg.get("parameters") or {})
    params.update(
        {
            "strategy": strategy,
            "ohlcv_from": src.name,
            "engine": "F1_veto_max20" if "F1" in strategy else "F0_dual",
        }
    )
    cfg["parameters"] = params
    if "validation" not in cfg:
        cfg["validation"] = {"monte_carlo": {"n_simulations": 1000}}
    (dst / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run(run_dir: Path) -> dict:
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if "initial_cash" not in cfg and "initial_capital" in cfg:
        cfg["initial_cash"] = cfg["initial_capital"]
    codes = list(cfg.get("codes") or [])
    data_map = _load_ohlcv(run_dir / "artifacts", codes)
    print(f"[{run_dir.name}] loaded {len(data_map)} symbols")
    if not data_map:
        raise SystemExit(f"no OHLCV in {run_dir}")

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"eng_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    cfg["codes"] = list(data_map.keys())
    cfg["source"] = cfg.get("source") if cfg.get("source") not in (None, "auto") else "tushare"
    # force tushare market engine for A-shares
    cfg["source"] = "tushare"
    cfg["_run_card_effective_sources"] = ["local_artifacts"]

    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year(cfg.get("interval", "1D"), "tushare")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "metrics": metrics,
        "validation": validation,
        "n_symbols": len(data_map),
        "is_rebalance_days": getattr(sig, "last_is_rebalance_days", None),
        "alpha_ids": list(getattr(sig, "alpha_ids", [])),
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


def _holdings(run_dir: Path) -> tuple[str, dict]:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    row = pos.loc[last]
    held = row[row > 0].sort_values(ascending=False)
    n = hold.sum(axis=1)
    invested = n[n > 0]
    stats = {
        "last_date": str(last.date()),
        "n_held": int(len(held)),
        "gross": float(held.sum()),
        "mean_n": float(invested.mean()) if len(invested) else 0.0,
        "median_n": float(invested.median()) if len(invested) else 0.0,
        "codes": list(held.index),
    }
    lines = ["| 代码 | 权重 |", "|------|------|"]
    for code, w in held.items():
        lines.append(f"| {code} | {float(w):.4f} |")
    return "\n".join(lines), stats


def _write_report(
    path: Path,
    panel_label: str,
    panel_key: str,
    f1_run: Path,
    f0_run: Path,
    f1: dict,
    f0: dict,
    other_panel: dict,
) -> None:
    m1, m0 = f1["metrics"], f0["metrics"]
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    hold_md, hstat = _holdings(f1_run)
    _, h0 = _holdings(f0_run)

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    other_f1 = other_panel["f1"]["metrics"]
    other_label = other_panel["label"]

    lines = []
    lines.append(f"# F1 最优方案报告 — {panel_label}")
    lines.append("")
    lines.append("**声明：研究/回测结果，非实盘建议。F1 在人形机器人样本内寻优，本报告含跨板块对照。**")
    lines.append("")
    lines.append("## 1. 运行概况")
    lines.append("")
    lines.append("| 项 | 内容 |")
    lines.append("|----|------|")
    lines.append(f"| 板块 | **{panel_label}** |")
    lines.append(f"| 策略 | F1 = F0(164+171/054) + `custom_max_inv` 底部 20% 否决新开仓 |")
    lines.append(f"| F1 run | `{f1_run.name}` |")
    lines.append(f"| 同池 F0 对照 | `{f0_run.name}` |")
    lines.append(f"| OHLCV 来源 | `{f1_run.name}` ← 复制自源 run artifacts |")
    lines.append(f"| 标的数 | {f1['n_symbols']} |")
    lines.append("| 区间 | 2024-01-01 → 2026-07-24 |")
    lines.append(f"| 因子 | `{', '.join(f1['alpha_ids'])}` |")
    lines.append(f"| is_rebalance_days | {f1.get('is_rebalance_days')} |")
    lines.append("")

    lines.append("## 2. F1 核心绩效")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    for k, pct in [
        ("final_value", False),
        ("total_return", True),
        ("annual_return", True),
        ("benchmark_return", True),
        ("excess_return", True),
        ("sharpe", False),
        ("max_drawdown", True),
        ("win_rate", True),
        ("profit_factor", False),
        ("trade_count", False),
        ("rebalance_count", False),
        ("avg_holding_days", False),
        ("rebalance_turnover_mean", False),
    ]:
        val = m1.get(k)
        if val is None:
            continue
        if k == "final_value":
            lines.append(f"| {k} | {_num(val, 2)} |")
        elif pct:
            lines.append(f"| {k} | {_pct(val)} |")
        elif k in ("trade_count", "rebalance_count"):
            lines.append(f"| {k} | {int(float(val))} |")
        else:
            lines.append(f"| {k} | {_num(val)} |")
    if v1:
        lines.append(f"| MC p_value_sharpe | {_num(v1.get('p_value_sharpe'))} |")
        lines.append(f"| MC n_simulations | {v1.get('n_simulations')} |")
    lines.append("")

    lines.append("### 必须承认")
    lines.append("")
    if float(m1["excess_return"]) < 0:
        lines.append(
            f"- **excess = {_pct(m1['excess_return'])} < 0**：未跑赢本板块等权基准 "
            f"({_pct(m1['benchmark_return'])})。"
        )
    else:
        lines.append(
            f"- excess = {_pct(m1['excess_return'])} > 0：本样本相对板块基准为正，仍须样本外复核。"
        )
    if v1 and float(v1.get("p_value_sharpe", 0)) > 0.2:
        lines.append(f"- MC p_sharpe = {_num(v1.get('p_value_sharpe'))} > 0.2，统计不显著。")
    elif v1:
        lines.append(f"- MC p_sharpe = {_num(v1.get('p_value_sharpe'))}（阈值 0.2）。")
    lines.append("")

    lines.append("## 3. 同板块：F1 vs F0")
    lines.append("")
    lines.append("| 指标 | F1 | F0 | F1−F0 |")
    lines.append("|------|----|----|-------|")
    for key, is_pct in [
        ("total_return", True),
        ("excess_return", True),
        ("sharpe", False),
        ("max_drawdown", True),
        ("win_rate", True),
        ("trade_count", False),
        ("rebalance_turnover_mean", False),
        ("benchmark_return", True),
    ]:
        a, b = m1[key], m0[key]
        d = float(a) - float(b)
        if is_pct:
            lines.append(f"| {key} | {_pct(a)} | {_pct(b)} | {_pct(d)} |")
        else:
            lines.append(f"| {key} | {_num(a)} | {_num(b)} | {_num(d)} |")
    if v1 and v0:
        lines.append(
            f"| MC p_sharpe | {_num(v1.get('p_value_sharpe'))} | {_num(v0.get('p_value_sharpe'))} | "
            f"{_num(float(v1.get('p_value_sharpe', 0)) - float(v0.get('p_value_sharpe', 0)))} |"
        )
    lines.append("")
    if d_excess > 0 and d_total > 0:
        lines.append(
            f"> **同板块结论：F1 相对 F0 正向**（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, "
            f"Δsharpe={_num(d_sharpe)}）。"
        )
    elif d_excess > 0:
        lines.append(
            f"> **同板块结论：F1 excess 优于 F0，但 total 未同步提升** "
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}）。"
        )
    else:
        lines.append(
            f"> **同板块结论：F1 未带来增量 / 迁移失败** "
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。"
            "不可因机器人样本表现好而默认创新药同样有效。"
        )
    lines.append("")

    lines.append(f"## 4. 跨板块：本报告 vs {other_label} 的 F1")
    lines.append("")
    lines.append("| 指标 | 本板块 F1 | 对照板块 F1 |")
    lines.append("|------|-----------|-------------|")
    for key, is_pct in [
        ("total_return", True),
        ("excess_return", True),
        ("sharpe", False),
        ("max_drawdown", True),
        ("benchmark_return", True),
        ("win_rate", True),
        ("trade_count", False),
    ]:
        a, b = m1[key], other_f1[key]
        if is_pct:
            lines.append(f"| {key} | {_pct(a)} | {_pct(b)} |")
        else:
            lines.append(f"| {key} | {_num(a)} | {_num(b)} |")
    lines.append("")

    lines.append("## 5. 最近调仓日持仓（F1）")
    lines.append("")
    lines.append(f"- 日期：{hstat['last_date']}；持仓 {hstat['n_held']} 只；权重和 {_num(hstat['gross'], 4)}")
    lines.append(f"- 有仓日平均持仓只数 {_num(hstat['mean_n'], 2)}（中位 {_num(hstat['median_n'], 2)}）")
    lines.append("")
    lines.append(hold_md)
    lines.append("")
    lines.append(
        f"同日 F0 持仓：`{', '.join(h0['codes'])}`（n={h0['n_held']}, gross={_num(h0['gross'], 4)}）"
    )
    lines.append("")

    lines.append("## 6. 白话结论")
    lines.append("")
    if panel_key == "robot":
        lines.append("1. 本报告是 F1 在寻优板块（人形机器人）上的正式复跑。")
        lines.append("2. 相对同池 F0，MAX 底部否决应继续显著占优（样本内）。")
        lines.append("3. 必须与创新药报告对照：若创新药 F1 不增益，则 F1 不是普适增强。")
        lines.append("4. excess/基准、MC p 仍按纪律披露。")
        lines.append("5. 过程约束（周频/换手帽/最小持有）应保持与 F0 一致。")
    else:
        lines.append("1. 本报告是 F1 在创新药池上的**样本外迁移**检验。")
        lines.append("2. 若相对同池 F0 无增益，应明文写「F1 未在创新药迁移成功」。")
        lines.append("3. 不可为追收益把否决改回 E 式对称质量硬过滤。")
        lines.append("4. 创新药政策/临床/估值波动是主要板块风险。")
        lines.append("5. 稳健默认仍可回退 F0；F1 仅在多板块验证后再升主策略。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"对照报告见另一板块 run；本 run artifacts：`{f1_run.as_posix()}`。")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    results = {}
    for p in PANELS:
        print("=" * 60, p["label"])
        _prepare(p["f0_run"], p["src"], F0_ENGINE_SRC, f"F0_{p['key']}")
        _prepare(p["f1_run"], p["src"], ENGINE_SRC, f"F1_{p['key']}")
        f0_info = _run(p["f0_run"])
        f1_info = _run(p["f1_run"])
        results[p["key"]] = {
            "label": p["label"],
            "f0": f0_info,
            "f1": f1_info,
            "f0_run": p["f0_run"],
            "f1_run": p["f1_run"],
        }
        print(
            p["key"],
            "F0 excess",
            f0_info["metrics"]["excess_return"],
            "F1 excess",
            f1_info["metrics"]["excess_return"],
        )

    # Cross reports
    _write_report(
        results["robot"]["f1_run"] / "REPORT_F1_robot.md",
        results["robot"]["label"],
        "robot",
        results["robot"]["f1_run"],
        results["robot"]["f0_run"],
        results["robot"]["f1"],
        results["robot"]["f0"],
        {
            "label": results["drug"]["label"],
            "f1": results["drug"]["f1"],
        },
    )
    _write_report(
        results["drug"]["f1_run"] / "REPORT_F1_drug.md",
        results["drug"]["label"],
        "drug",
        results["drug"]["f1_run"],
        results["drug"]["f0_run"],
        results["drug"]["f1"],
        results["drug"]["f0"],
        {
            "label": results["robot"]["label"],
            "f1": results["robot"]["f1"],
        },
    )

    summary = {
        k: {
            "label": v["label"],
            "f0": v["f0"]["metrics"],
            "f1": v["f1"]["metrics"],
            "f0_mc": (v["f0"].get("validation") or {}).get("monte_carlo"),
            "f1_mc": (v["f1"].get("validation") or {}).get("monte_carlo"),
            "f1_run": v["f1_run"].name,
            "f0_run": v["f0_run"].name,
        }
        for k, v in results.items()
    }
    out = _AGENT_ROOT / "runs" / "20260725_184000_F1_two_panels_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print("summary", out)


if __name__ == "__main__":
    main()
