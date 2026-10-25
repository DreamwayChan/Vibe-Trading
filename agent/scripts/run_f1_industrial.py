"""Re-run F0 + F1 (MAX bottom-20% veto) on industrial-machine (BK1004) cached OHLCV.

Fixes the failed agent run that expected factor columns in data_map: the working
engines compute gtja191_* / custom_max_inv via Registry from OHLCV panels.
"""

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

ENGINE_F1 = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "veto_max_20" / "code" / "signal_engine.py"
)
ENGINE_F0 = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "F0" / "code" / "signal_engine.py"
)

# Failed agent run that already downloaded BK1004 OHLCV (78 A-share names).
SRC = _AGENT_ROOT / "runs" / "20260726_001235_72_7ae06c"
F1_RUN = _AGENT_ROOT / "runs" / "20260726_084500_01_F1_machine"
F0_RUN = _AGENT_ROOT / "runs" / "20260726_084500_01_F0_machine"


def _load_ohlcv(art: Path, codes: list[str]) -> dict[str, pd.DataFrame]:
    data_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        data_map[code] = df[keep].copy()
    if not data_map:
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
    if "initial_cash" not in cfg:
        cfg["initial_cash"] = 1_000_000
    params = dict(cfg.get("parameters") or {})
    params.update(
        {
            "strategy": strategy,
            "panel": "工业母机 BK1004",
            "ohlcv_from": src.name,
            "engine": "F1_veto_max20" if "F1" in strategy else "F0_dual",
            "fix": "Registry computes factors from OHLCV (no pre-injected alpha columns)",
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
    if "initial_cash" not in cfg:
        cfg["initial_cash"] = 1_000_000
    codes = list(cfg.get("codes") or [])
    data_map = _load_ohlcv(run_dir / "artifacts", codes)
    print(f"[{run_dir.name}] loaded {len(data_map)} symbols")
    if not data_map:
        raise SystemExit(f"no OHLCV in {run_dir}")

    # Quick factor smoke check before the full backtest.
    from src.factors.registry import Registry

    panel = {
        "close": pd.DataFrame({c: data_map[c]["close"] for c in data_map}),
        "open": pd.DataFrame({c: data_map[c]["open"] for c in data_map}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in data_map}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in data_map}),
        "volume": pd.DataFrame({c: data_map[c]["volume"] for c in data_map}),
    }
    reg = Registry()
    for aid in ("gtja191_164", "gtja191_171", "gtja191_054", "custom_max_inv"):
        raw = reg.compute(aid, panel)
        nn = int(raw.notna().to_numpy().sum()) if raw is not None else 0
        print(f"[{run_dir.name}] factor {aid}: non_null={nn}")
        if nn <= 0:
            raise SystemExit(f"factor {aid} produced no values")

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"eng_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    cfg["codes"] = list(data_map.keys())
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


def _write_report(f1_run: Path, f0_run: Path, f1: dict, f0: dict) -> None:
    m1, m0 = f1["metrics"], f0["metrics"]
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    hold_md, hstat = _holdings(f1_run)
    _, h0 = _holdings(f0_run)

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    lines = [
        "# F1 最优方案报告 — 工业母机 BK1004",
        "",
        "**声明：研究/回测结果，非实盘建议。F1 结构自人形机器人样本寻优后迁移至此；本报告为工业母机复跑。**",
        "",
        "## 1. 运行概况",
        "",
        "| 项 | 内容 |",
        "|----|------|",
        "| 板块 | **工业母机 BK1004** |",
        "| 策略 | F1 = F0(164+171/054) + `custom_max_inv` 底部 20% 否决新开仓 |",
        f"| F1 run | `{f1_run.name}` |",
        f"| 同池 F0 对照 | `{f0_run.name}` |",
        f"| OHLCV 来源 | `{SRC.name}`（失败 run 已缓存的 78 只 A 股） |",
        f"| 标的数 | {f1['n_symbols']} |",
        "| 区间 | 配置 2024-01-01 → 2026-07-24（实际以可得 OHLCV 为准） |",
        f"| 因子 | `{', '.join(f1['alpha_ids'])}` |",
        f"| is_rebalance_days | {f1.get('is_rebalance_days')} |",
        "| 修复 | 使用 Registry 从 OHLCV 计算因子；不再依赖 data_map 预注入因子列 |",
        "",
        "## 2. F1 核心绩效",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
    ]
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
        lines.append("- **F1 结构未在工业母机迁移成功**（相对板块基准为负）。")
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
            f"> **同板块结论：F1 未在工业母机带来增量** "
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。"
        )
    lines.append("")

    lines.append("## 4. 最近调仓日持仓（F1）")
    lines.append("")
    lines.append(
        f"- 日期：{hstat['last_date']}；持仓 {hstat['n_held']} 只；权重和 {_num(hstat['gross'], 4)}"
    )
    lines.append(
        f"- 有仓日平均持仓只数 {_num(hstat['mean_n'], 2)}（中位 {_num(hstat['median_n'], 2)}）"
    )
    lines.append("")
    lines.append(hold_md)
    lines.append("")
    lines.append(
        f"同日 F0 持仓：`{', '.join(h0['codes'])}`（n={h0['n_held']}, gross={_num(h0['gross'], 4)}）"
    )
    lines.append("")

    lines.append("## 5. 白话结论")
    lines.append("")
    lines.append(
        "1. `custom_max_inv` 是反彩票底部否决（拦新开），不是 E 式对称质量主确认；未使用反转/吸筹。"
    )
    lines.append("2. 相对同池 F0 的 Δexcess / Δsharpe 是迁移成败的第一判据。")
    lines.append("3. 工业母机订单/设备周期与国产替代叙事波动大，单板块结果易过拟合。")
    lines.append("4. 若 excess<0 或相对 F0 无增量，应回退 F0，禁止为追收益擅自加反转/扩因子。")
    lines.append("5. 研究/回测，非实盘建议。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"本 run artifacts：`{f1_run.as_posix()}`。")

    path = f1_run / "REPORT_F1_machine.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"missing source OHLCV run: {SRC}")
    if not ENGINE_F1.exists() or not ENGINE_F0.exists():
        raise SystemExit("missing F0/F1 engine sources under _research_custom_positive")

    print("=" * 60, "工业母机 F0")
    _prepare(F0_RUN, SRC, ENGINE_F0, "F0_machine")
    f0_info = _run(F0_RUN)
    print("F0 excess", f0_info["metrics"].get("excess_return"), "trades", f0_info["metrics"].get("trade_count"))

    print("=" * 60, "工业母机 F1")
    _prepare(F1_RUN, SRC, ENGINE_F1, "F1_machine")
    f1_info = _run(F1_RUN)
    print("F1 excess", f1_info["metrics"].get("excess_return"), "trades", f1_info["metrics"].get("trade_count"))

    _write_report(F1_RUN, F0_RUN, f1_info, f0_info)

    summary = {
        "panel": "工业母机 BK1004",
        "src": SRC.name,
        "f0_run": F0_RUN.name,
        "f1_run": F1_RUN.name,
        "f0": f0_info["metrics"],
        "f1": f1_info["metrics"],
        "f0_mc": (f0_info.get("validation") or {}).get("monte_carlo"),
        "f1_mc": (f1_info.get("validation") or {}).get("monte_carlo"),
        "f0_is_rebalance_days": f0_info.get("is_rebalance_days"),
        "f1_is_rebalance_days": f1_info.get("is_rebalance_days"),
    }
    out = _AGENT_ROOT / "runs" / "20260726_084500_F1_machine_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print("summary", out)


if __name__ == "__main__":
    main()
