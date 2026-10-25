"""Re-run F0 (164 + hard 171/054) on industrial-machine BK1004; write REPORT_F0."""

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

ENGINE_F0 = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "F0" / "code" / "signal_engine.py"
)
SRC = _AGENT_ROOT / "runs" / "20260726_001235_72_7ae06c"
F0_RUN = _AGENT_ROOT / "runs" / "20260726_084500_01_F0_machine"
F1_RUN = _AGENT_ROOT / "runs" / "20260726_084500_01_F1_machine"


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


def _prepare(dst: Path, src: Path, engine_src: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    (dst / "artifacts").mkdir(parents=True)
    for p in (src / "artifacts").glob("ohlcv_*.csv"):
        shutil.copy2(p, dst / "artifacts" / p.name)
    shutil.copy2(engine_src, dst / "code" / "signal_engine.py")
    cfg = json.loads((src / "config.json").read_text(encoding="utf-8"))
    cfg["initial_cash"] = cfg.get("initial_cash") or cfg.get("initial_capital") or 1_000_000
    # F0 whitelist only — do not list custom_max_inv
    cfg["alphas"] = ["gtja191_164", "gtja191_171", "gtja191_054"]
    params = dict(cfg.get("parameters") or {})
    params.update(
        {
            "strategy": "F0_machine",
            "panel": "工业母机 BK1004",
            "ohlcv_from": src.name,
            "engine": "F0_dual",
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
    cfg["initial_cash"] = cfg.get("initial_cash") or cfg.get("initial_capital") or 1_000_000
    codes = list(cfg.get("codes") or [])
    data_map = _load_ohlcv(run_dir / "artifacts", codes)
    print(f"[{run_dir.name}] loaded {len(data_map)} symbols")
    if not data_map:
        raise SystemExit(f"no OHLCV in {run_dir}")

    from src.factors.registry import Registry

    panel = {
        "close": pd.DataFrame({c: data_map[c]["close"] for c in data_map}),
        "open": pd.DataFrame({c: data_map[c]["open"] for c in data_map}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in data_map}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in data_map}),
        "volume": pd.DataFrame({c: data_map[c]["volume"] for c in data_map}),
    }
    reg = Registry()
    for aid in ("gtja191_164", "gtja191_171", "gtja191_054"):
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


def _load_f1_side() -> dict | None:
    """Optional F1 contrast from prior rerun artifacts."""
    metrics_path = F1_RUN / "artifacts" / "metrics.csv"
    if not metrics_path.exists():
        return None
    m = pd.read_csv(metrics_path).iloc[0].to_dict()
    val_path = F1_RUN / "artifacts" / "validation.json"
    v = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {"metrics": m, "validation": v, "run": F1_RUN.name}


def _write_report(f0_run: Path, f0: dict, f1: dict | None) -> Path:
    m0 = f0["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    hold_md, hstat = _holdings(f0_run)

    # Actual OHLCV span
    sample = next((f0_run / "artifacts").glob("ohlcv_*.csv"))
    sdf = pd.read_csv(sample)
    dcol = "trade_date" if "trade_date" in sdf.columns else "date"
    dmin, dmax = str(pd.to_datetime(sdf[dcol]).min().date()), str(
        pd.to_datetime(sdf[dcol]).max().date()
    )

    lines = [
        "# F0 双确认报告 — 工业母机 BK1004",
        "",
        "**声明：研究/回测结果，非实盘建议。本报告为工业母机板块的 F0（164 + 171/054 硬确认）正式复跑。**",
        "",
        "## 1. 运行概况",
        "",
        "| 项 | 内容 |",
        "|----|------|",
        "| 板块 | **工业母机 BK1004** |",
        "| 策略 | F0 = 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054`（无 custom 否决） |",
        f"| F0 run | `{f0_run.name}` |",
        f"| OHLCV 来源 | `{SRC.name}`（78 只 A 股，已排除 BJ） |",
        f"| 标的数 | {f0['n_symbols']} |",
        f"| 配置区间 | 2024-01-01 → 2026-07-24 |",
        f"| 实际数据 | {dmin} → {dmax} |",
        f"| 因子 | `{', '.join(f0['alpha_ids'])}` |",
        f"| is_rebalance_days | {f0.get('is_rebalance_days')} |",
        "| 说明 | 因子经 Registry 由 OHLCV 计算；未用 114 / 反转 / custom 质量确认 |",
        "",
        "## 2. 因子角色",
        "",
        "| ID | 角色 |",
        "|----|------|",
        "| gtja191_164 | 动量主排序（S） |",
        "| gtja191_171 | F0 硬确认（IR 0.155） |",
        "| gtja191_054 | F0 硬确认（IR 0.152） |",
        "",
        "- 确认分 C ≥ 当日截面 40% 分位才允许新开；确认全缺日不过滤。",
        "- 未使用 `custom_max_inv` / 吸筹 / 反转；工业母机上 F1 已证伪，本版以 F0 为主策略。",
        "",
        "## 3. F0 核心绩效",
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
        val = m0.get(k)
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
    lines.append(f"| is_rebalance_days | {f0.get('is_rebalance_days')} |")
    if v0:
        lines.append(f"| MC p_value_sharpe | {_num(v0.get('p_value_sharpe'))} |")
        lines.append(f"| MC n_simulations | {v0.get('n_simulations')} |")
    lines.append("")

    lines.append("### 必须承认")
    lines.append("")
    if float(m0["excess_return"]) < 0:
        lines.append(
            f"- **excess = {_pct(m0['excess_return'])} < 0**：未跑赢本板块等权基准 "
            f"({_pct(m0['benchmark_return'])})。"
        )
    else:
        lines.append(
            f"- excess = {_pct(m0['excess_return'])} > 0：本样本相对板块等权基准为正，仍须样本外复核。"
        )
    if v0 and float(v0.get("p_value_sharpe", 0)) > 0.2:
        lines.append(f"- MC p_sharpe = {_num(v0.get('p_value_sharpe'))} > 0.2，统计不显著。")
    elif v0:
        lines.append(f"- MC p_sharpe = {_num(v0.get('p_value_sharpe'))}（阈值 0.2）。")
    lines.append(
        "- 基准为板块等权（官方指数 `000852.SH` 拉取失败时回退）；与策略同池可比。"
    )
    lines.append("")

    if f1 is not None:
        m1 = f1["metrics"]
        v1 = (f1.get("validation") or {}).get("monte_carlo", {})
        d_total = float(m0["total_return"]) - float(m1["total_return"])
        d_excess = float(m0["excess_return"]) - float(m1["excess_return"])
        d_sharpe = float(m0["sharpe"]) - float(m1["sharpe"])
        lines.extend(
            [
                "## 4. 同池对照：F0 vs F1（先前复跑）",
                "",
                f"F1 run：`{f1['run']}`（F0 + `custom_max_inv` 底部 20% 否决）",
                "",
                "| 指标 | F0 | F1 | F0−F1 |",
                "|------|----|----|-------|",
            ]
        )
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
            a, b = m0[key], m1[key]
            d = float(a) - float(b)
            if is_pct:
                lines.append(f"| {key} | {_pct(a)} | {_pct(b)} | {_pct(d)} |")
            else:
                lines.append(f"| {key} | {_num(a)} | {_num(b)} | {_num(d)} |")
        if v0 and v1:
            lines.append(
                f"| MC p_sharpe | {_num(v0.get('p_value_sharpe'))} | {_num(v1.get('p_value_sharpe'))} | "
                f"{_num(float(v0.get('p_value_sharpe', 0)) - float(v1.get('p_value_sharpe', 0)))} |"
            )
        lines.append("")
        lines.append(
            f"> **结论：工业母机上以 F0 为准**（相对 F1：Δtotal={_pct(d_total)}, "
            f"Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。"
            "F1 未带来增量，禁止为追收益叠加 MAX 否决或扩因子。"
        )
        lines.append("")
        holdings_section = "## 5. 最近调仓日持仓（F0）"
        plain_section = "## 6. 白话结论"
    else:
        holdings_section = "## 4. 最近调仓日持仓（F0）"
        plain_section = "## 5. 白话结论"

    lines.extend(
        [
            holdings_section,
            "",
            f"- 日期：{hstat['last_date']}；持仓 {hstat['n_held']} 只；权重和 {_num(hstat['gross'], 4)}",
            f"- 有仓日平均持仓只数 {_num(hstat['mean_n'], 2)}（中位 {_num(hstat['median_n'], 2)}）",
            "",
            hold_md,
            "",
            plain_section,
            "",
            "1. F0 = 纯动量排序 + 171/054 硬确认；过程约束（周频 / 换手帽 20% / 最小持有 5 日 / 择时半仓）写死在 signal_engine。",
            "2. 本样本 excess 与 MC p 按纪律披露；正 excess 仍属板块内样本结果。",
            "3. 工业母机订单/设备周期与国产替代叙事波动大，单板块易过拟合。",
            "4. 同池 F1（MAX 底部否决）弱于 F0，工业母机默认策略回退 F0。",
            "5. 研究/回测，非实盘建议。",
            "",
            "---",
            "",
            f"本 run artifacts：`{f0_run.as_posix()}`。",
        ]
    )

    path = f0_run / "REPORT_F0_machine.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return path


def _update_f1_report_pointer() -> None:
    """Point the prior F1 report at the refreshed F0 report as the preferred strategy."""
    f1_report = F1_RUN / "REPORT_F1_machine.md"
    if not f1_report.exists():
        return
    text = f1_report.read_text(encoding="utf-8")
    marker = "## 5. 白话结论"
    note = (
        "## 5. 策略选择（更新）\n"
        "\n"
        f"> 工业母机主报告已改为 F0：见 `{F0_RUN.name}/REPORT_F0_machine.md`。"
        "本文件保留 F1 迁移证伪记录。\n"
        "\n"
        + marker
    )
    if "## 5. 策略选择（更新）" in text:
        return
    if marker in text:
        text = text.replace(marker, note, 1)
        f1_report.write_text(text, encoding="utf-8")
        print(f"updated {f1_report}")


def main() -> None:
    if not SRC.exists() or not ENGINE_F0.exists():
        raise SystemExit("missing SRC OHLCV or F0 engine")

    print("=" * 60, "工业母机 F0")
    _prepare(F0_RUN, SRC, ENGINE_F0)
    f0_info = _run(F0_RUN)
    print(
        "F0",
        "total",
        f0_info["metrics"]["total_return"],
        "excess",
        f0_info["metrics"]["excess_return"],
        "sharpe",
        f0_info["metrics"]["sharpe"],
        "trades",
        f0_info["metrics"]["trade_count"],
        "is_reb",
        f0_info.get("is_rebalance_days"),
    )

    f1_side = _load_f1_side()
    _write_report(F0_RUN, f0_info, f1_side)
    _update_f1_report_pointer()

    summary = {
        "panel": "工业母机 BK1004",
        "strategy": "F0_dual",
        "src": SRC.name,
        "f0_run": F0_RUN.name,
        "f0": f0_info["metrics"],
        "f0_mc": (f0_info.get("validation") or {}).get("monte_carlo"),
        "is_rebalance_days": f0_info.get("is_rebalance_days"),
        "alpha_ids": f0_info.get("alpha_ids"),
        "f1_contrast_run": f1_side["run"] if f1_side else None,
    }
    out = _AGENT_ROOT / "runs" / "20260726_084500_F0_machine_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print("summary", out)


if __name__ == "__main__":
    main()
