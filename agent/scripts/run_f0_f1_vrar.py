"""Build VR/AR (东财 BK0722 虚拟现实) panel, run F0+F1, write frontend reports.

Universe source: Eastmoney concept board BK0722 (虚拟现实), fetched via browser
when push2 is blocked from the local Python process. BJ names are dropped.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
ENGINE_F1 = (
    _AGENT_ROOT
    / "runs"
    / "_research_custom_positive"
    / "veto_max_20"
    / "code"
    / "signal_engine.py"
)

SRC_RUN = _AGENT_ROOT / "runs" / "20260726_123500_00_VRAR_universe"
F0_RUN = _AGENT_ROOT / "runs" / "20260726_123500_01_F0_vrar"
F1_RUN = _AGENT_ROOT / "runs" / "20260726_123500_02_F1_vrar"
UNIVERSE_JSON = (
    _AGENT_ROOT / "runs" / "_tmp_vrar_universe" / "bk0722_from_browser.json"
)

START_DATE = "2024-01-01"
END_DATE = "2026-07-24"
MIN_BARS = 120
MC_SIMS = 1000
PANEL_LABEL = "虚拟现实 BK0722"


def _is_bj(code: str) -> bool:
    digits = code.split(".")[0]
    return digits.startswith(("4", "8", "92")) or code.upper().endswith(".BJ")


def _normalize_universe(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for item in raw:
        code = str(item.get("ts_code") or "").upper()
        name = str(item.get("name") or "")
        if not code or "." not in code:
            continue
        # Fix BJ mislabeled as .SZ (92xxxx)
        digits = code.split(".")[0]
        if digits.startswith("92"):
            continue
        if _is_bj(code):
            continue
        rows.append({"ts_code": code, "name": name, "board": PANEL_LABEL})
    df = pd.DataFrame(rows).drop_duplicates("ts_code").sort_values("ts_code")
    return df.reset_index(drop=True)


def _fetch_one_tushare(code: str, start: str, end: str):
    from backtest.loaders.tushare import DataLoader

    loader = DataLoader()
    try:
        data = loader.fetch([code], start, end, interval="1D")
        df = data.get(code)
        if df is None or df.empty:
            return code, None, "empty"
        return code, df, ""
    except Exception as exc:  # noqa: BLE001
        return code, None, str(exc)


def _fetch_one_akshare(code: str, start: str, end: str):
    import akshare as ak

    symbol = code.split(".")[0]
    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust="qfq",
        )
    except Exception as exc:  # noqa: BLE001
        return code, None, str(exc)
    if raw is None or raw.empty:
        return code, None, "empty"
    df = raw.rename(
        columns={
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
        }
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date").sort_index()
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[keep].dropna(subset=["open", "high", "low", "close"])
    return code, df, ""


def fetch_ohlcv(codes: list[str], art: Path) -> dict[str, pd.DataFrame]:
    art.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    pending = []
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if path.exists():
            df = pd.read_csv(path, parse_dates=["trade_date"]).set_index("trade_date")
            df = df.loc[START_DATE:END_DATE]
            if len(df) >= MIN_BARS:
                data_map[code] = df
                continue
        pending.append(code)

    print(f"[fetch] cached={len(data_map)} pending={len(pending)}")
    if not pending:
        return data_map

    t0 = time.time()
    ok = fail = 0
    # Prefer tushare daily (works with current tokens); fall back to akshare.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one_tushare, c, START_DATE, END_DATE): c for c in pending}
        for fut in as_completed(futs):
            code, df, err = fut.result()
            if df is None or len(df) < MIN_BARS:
                # akshare fallback for this code
                code2, df2, err2 = _fetch_one_akshare(code, START_DATE, END_DATE)
                if df2 is None or len(df2) < MIN_BARS:
                    fail += 1
                    if fail <= 10:
                        print(f"  skip {code}: {err or err2 or 'short'}")
                    continue
                df = df2
            out = df.copy()
            out.index.name = "trade_date"
            out.to_csv(art / f"ohlcv_{code}.csv")
            data_map[code] = out
            ok += 1
            if ok % 25 == 0:
                print(f"  fetched {ok}/{len(pending)} fail={fail} {time.time()-t0:.1f}s")
    print(f"[fetch] done ok+cache={len(data_map)} new_ok={ok} fail={fail}")
    return data_map


def _prepare(dst: Path, codes: list[str], data_map: dict, engine_src: Path, strategy: str) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    art = dst / "artifacts"
    art.mkdir(parents=True)
    for code in codes:
        out = data_map[code].copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")
    shutil.copy2(engine_src, dst / "code" / "signal_engine.py")
    alphas = ["gtja191_164", "gtja191_171", "gtja191_054"]
    if "F1" in strategy:
        alphas = alphas + ["custom_max_inv"]
    cfg = {
        "codes": codes,
        "source": "tushare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": 1_000_000,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": alphas,
        "parameters": {
            "strategy": strategy,
            "panel": PANEL_LABEL,
            "engine": "F1_veto_max20" if "F1" in strategy else "F0_dual",
            "universe": "东财概念 BK0722 虚拟现实（排除北交所）",
        },
        "validation": {"monte_carlo": {"n_simulations": MC_SIMS}},
    }
    (dst / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run(run_dir: Path, data_map: dict) -> dict:
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    codes = [c for c in cfg["codes"] if c in data_map]
    subset = {c: data_map[c] for c in codes}
    print(f"[{run_dir.name}] symbols={len(subset)}")

    from src.factors.registry import Registry

    panel = {
        "close": pd.DataFrame({c: subset[c]["close"] for c in subset}),
        "open": pd.DataFrame({c: subset[c]["open"] for c in subset}),
        "high": pd.DataFrame({c: subset[c]["high"] for c in subset}),
        "low": pd.DataFrame({c: subset[c]["low"] for c in subset}),
        "volume": pd.DataFrame({c: subset[c]["volume"] for c in subset}),
    }
    reg = Registry()
    need = ["gtja191_164", "gtja191_171", "gtja191_054"]
    if "F1" in str(cfg.get("parameters", {}).get("strategy", "")):
        need.append("custom_max_inv")
    for aid in need:
        raw = reg.compute(aid, panel)
        nn = int(raw.notna().to_numpy().sum()) if raw is not None else 0
        print(f"  factor {aid}: non_null={nn}")
        if nn <= 0:
            raise SystemExit(f"factor {aid} empty")

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"eng_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    cfg["codes"] = list(subset.keys())
    cfg["source"] = "tushare"
    cfg["_run_card_effective_sources"] = ["local_artifacts"]
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    engine.run_backtest(cfg, _AutoLoader(subset), sig, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "metrics": metrics,
        "validation": validation,
        "n_symbols": len(subset),
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


def _holdings(run_dir: Path, name_map: dict[str, str]) -> tuple[str, dict]:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    held = pos.loc[last]
    held = held[held > 0].sort_values(ascending=False)
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
    lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
    for i, (code, w) in enumerate(held.items(), 1):
        lines.append(f"| {i} | {code} | {name_map.get(code, '')} | {float(w):.4f} |")
    return "\n".join(lines), stats


def _write_reports(f0: dict, f1: dict, name_map: dict[str, str], listed_n: int) -> None:
    m0, m1 = f0["metrics"], f1["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(F0_RUN, name_map)
    hold1_md, h1 = _holdings(F1_RUN, name_map)
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    # Actual data range from equity
    eq = pd.read_csv(F0_RUN / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    report_f0 = f"""# F0 双确认报告 — 虚拟现实 BK0722

**声明：研究/回测结果，非实盘建议。本报告为 VR/AR/头显相关板块（东财概念「虚拟现实」BK0722）的 F0（164 + 171/054 硬确认）正式复跑。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（VR眼镜 / 虚拟现实概念） |
| 策略 | F0 = 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054`（无 custom 否决） |
| F0 run | `{F0_RUN.name}` |
| 同池 F1 对照 | `{F1_RUN.name}` |
| 成分来源 | 东财概念 BK0722；排除北交所；原始成分 {listed_n} → 有效 OHLCV {f0['n_symbols']} |
| 配置区间 | {START_DATE} → {END_DATE} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `gtja191_164, gtja191_171, gtja191_054` |
| is_rebalance_days | {f0.get('is_rebalance_days')} |
| 说明 | 因子经 Registry 由 OHLCV 计算；未用 114 / 反转 / custom |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_171 | F0 硬确认（IR 0.155） |
| gtja191_054 | F0 硬确认（IR 0.152） |

- 确认分 C ≥ 当日截面 40% 分位才允许新开；确认全缺日不过滤。

## 3. F0 核心绩效

| 指标 | 数值 |
|------|------|
| final_value | {_num(m0['final_value'], 2)} |
| total_return | {_pct(m0['total_return'])} |
| annual_return | {_pct(m0['annual_return'])} |
| benchmark_return | {_pct(m0['benchmark_return'])} |
| excess_return | {_pct(m0['excess_return'])} |
| sharpe | {_num(m0['sharpe'])} |
| max_drawdown | {_pct(m0['max_drawdown'])} |
| win_rate | {_pct(m0['win_rate'])} |
| profit_factor | {_num(m0['profit_factor'])} |
| trade_count | {_num(m0.get('trade_count'), 0)} |
| rebalance_count | {_num(m0.get('rebalance_count'), 0)} |
| avg_holding_days | {_num(m0.get('avg_holding_days'))} |
| rebalance_turnover_mean | {_num(m0.get('rebalance_turnover_mean'))} |
| is_rebalance_days | {f0.get('is_rebalance_days')} |
| MC p_value_sharpe | {_num(v0.get('p_value_sharpe'))} |
| MC n_simulations | {v0.get('n_simulations', MC_SIMS)} |

### 必须承认

"""
    if float(m0["excess_return"]) < 0:
        report_f0 += f"- excess = {_pct(m0['excess_return'])} < 0：相对板块等权基准为负。\n"
    else:
        report_f0 += f"- excess = {_pct(m0['excess_return'])} > 0：本样本相对板块等权基准为正，仍须样本外复核。\n"
    p0 = v0.get("p_value_sharpe")
    if p0 is not None and float(p0) > 0.2:
        report_f0 += f"- MC p_sharpe = {_num(p0)} > 0.2。\n"
    else:
        report_f0 += f"- MC p_sharpe = {_num(p0)}（阈值 0.2）。\n"

    report_f0 += f"""
## 4. 同池对照：F0 vs F1

F1 run：`{F1_RUN.name}`（F0 + `custom_max_inv` 底部 20% 否决）

| 指标 | F0 | F1 | F0−F1 |
|------|----|----|-------|
| total_return | {_pct(m0['total_return'])} | {_pct(m1['total_return'])} | {_pct(-d_total)} |
| excess_return | {_pct(m0['excess_return'])} | {_pct(m1['excess_return'])} | {_pct(-d_excess)} |
| sharpe | {_num(m0['sharpe'])} | {_num(m1['sharpe'])} | {_num(-d_sharpe)} |
| max_drawdown | {_pct(m0['max_drawdown'])} | {_pct(m1['max_drawdown'])} | — |
| trade_count | {_num(m0.get('trade_count'), 0)} | {_num(m1.get('trade_count'), 0)} | — |
| rebalance_turnover_mean | {_num(m0.get('rebalance_turnover_mean'))} | {_num(m1.get('rebalance_turnover_mean'))} | — |
| benchmark_return | {_pct(m0['benchmark_return'])} | {_pct(m1['benchmark_return'])} | 0.00% |
| MC p_sharpe | {_num(v0.get('p_value_sharpe'))} | {_num(v1.get('p_value_sharpe'))} | — |

"""
    if d_excess > 0:
        report_f0 += (
            f"> **对照提示：同池 F1 excess 高于 F0**（Δexcess={_pct(d_excess)}）。"
            "详见 F1 报告。\n"
        )
    else:
        report_f0 += (
            f"> **结论：本板块以 F0 为对照基线**；F1 未带来增量"
            f"（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。\n"
        )

    report_f0 += f"""
## 5. 最近调仓日持仓（F0）

- 日期：{h0['last_date']}；持仓 {h0['n_held']} 只；权重和 {_num(h0['gross'])}
- 有仓日平均持仓只数 {_num(h0['mean_n'], 2)}（中位 {_num(h0['median_n'], 2)}）

{hold0_md}

## 6. 白话结论

1. F0 = 纯动量排序 + 171/054 硬确认；周频 / 换手帽 20% / 最小持有 5 日 / 择时半仓写死在 signal_engine。
2. VR/AR/头显主题受消费电子景气与产品周期驱动，波动大，单板块易过拟合。
3. 成分取自东财「虚拟现实」概念，覆盖光学/结构件/整机代工/内容等链条，非纯净「头显整机」清单。
4. 相对同池 F1 的 Δexcess 决定是否叠加 MAX 否决；无增量则回退 F0。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F0_RUN.resolve()}`。
"""

    report_f1 = f"""# F1 MAX否决报告 — 虚拟现实 BK0722

**声明：研究/回测结果，非实盘建议。F1 = F0 + `custom_max_inv` 底部 20% 否决；迁移至 VR/AR/头显相关板块（东财 BK0722）。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（VR眼镜 / 虚拟现实概念） |
| 策略 | F1 = F0(164+171/054) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1 run | `{F1_RUN.name}` |
| 同池 F0 对照 | `{F0_RUN.name}` |
| 成分来源 | 东财概念 BK0722；排除北交所；有效 OHLCV {f1['n_symbols']} |
| 配置区间 | {START_DATE} → {END_DATE} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `gtja191_164, gtja191_171, gtja191_054, custom_max_inv` |
| is_rebalance_days | {f1.get('is_rebalance_days')} |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_171 | F0 硬确认（IR 0.155） |
| gtja191_054 | F0 硬确认（IR 0.152） |
| custom_max_inv | 反彩票底部否决（仅底部 20%，拦新开；不是质量主确认） |

- 未使用反转 / 吸筹；未把 max_inv 做成 E 式对称质量过滤。

## 3. F1 核心绩效

| 指标 | 数值 |
|------|------|
| final_value | {_num(m1['final_value'], 2)} |
| total_return | {_pct(m1['total_return'])} |
| annual_return | {_pct(m1['annual_return'])} |
| benchmark_return | {_pct(m1['benchmark_return'])} |
| excess_return | {_pct(m1['excess_return'])} |
| sharpe | {_num(m1['sharpe'])} |
| max_drawdown | {_pct(m1['max_drawdown'])} |
| win_rate | {_pct(m1['win_rate'])} |
| profit_factor | {_num(m1['profit_factor'])} |
| trade_count | {_num(m1.get('trade_count'), 0)} |
| rebalance_count | {_num(m1.get('rebalance_count'), 0)} |
| avg_holding_days | {_num(m1.get('avg_holding_days'))} |
| rebalance_turnover_mean | {_num(m1.get('rebalance_turnover_mean'))} |
| is_rebalance_days | {f1.get('is_rebalance_days')} |
| MC p_value_sharpe | {_num(v1.get('p_value_sharpe'))} |
| MC n_simulations | {v1.get('n_simulations', MC_SIMS)} |

### 必须承认

"""
    if float(m1["excess_return"]) < 0:
        report_f1 += (
            f"- excess = {_pct(m1['excess_return'])} < 0："
            f"未跑赢板块等权基准（{_pct(m1['benchmark_return'])}）。\n"
        )
        report_f1 += "- **F1 结构未在 VR/AR/头显迁移成功**（相对板块基准为负）。\n"
    else:
        report_f1 += (
            f"- excess = {_pct(m1['excess_return'])} > 0：本样本相对板块基准为正，仍须样本外复核。\n"
        )
    p1 = v1.get("p_value_sharpe")
    if p1 is not None and float(p1) > 0.2:
        report_f1 += f"- MC p_sharpe = {_num(p1)} > 0.2，统计不显著。\n"
    else:
        report_f1 += f"- MC p_sharpe = {_num(p1)}（阈值 0.2）。\n"
    if d_excess <= 0:
        report_f1 += (
            f"- **F1 未在 VR/AR/头显带来增量**"
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。\n"
        )

    report_f1 += f"""
## 4. 同板块：F1 vs F0

| 指标 | F1 | F0 | F1−F0 |
|------|----|----|-------|
| total_return | {_pct(m1['total_return'])} | {_pct(m0['total_return'])} | {_pct(d_total)} |
| excess_return | {_pct(m1['excess_return'])} | {_pct(m0['excess_return'])} | {_pct(d_excess)} |
| sharpe | {_num(m1['sharpe'])} | {_num(m0['sharpe'])} | {_num(d_sharpe)} |
| max_drawdown | {_pct(m1['max_drawdown'])} | {_pct(m0['max_drawdown'])} | — |
| win_rate | {_pct(m1['win_rate'])} | {_pct(m0['win_rate'])} | — |
| trade_count | {_num(m1.get('trade_count'), 0)} | {_num(m0.get('trade_count'), 0)} | — |
| rebalance_turnover_mean | {_num(m1.get('rebalance_turnover_mean'))} | {_num(m0.get('rebalance_turnover_mean'))} | — |
| benchmark_return | {_pct(m1['benchmark_return'])} | {_pct(m0['benchmark_return'])} | 0.00% |
| MC p_sharpe | {_num(v1.get('p_value_sharpe'))} | {_num(v0.get('p_value_sharpe'))} | — |

"""
    if d_excess > 0 and d_total > 0:
        report_f1 += (
            f"> **同板块结论：F1 相对 F0 正向**"
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。\n"
        )
    else:
        report_f1 += (
            f"> **同板块结论：F1 未在 VR/AR/头显带来增量**"
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。\n"
        )

    report_f1 += f"""
## 5. 最近调仓日持仓（F1）

- 日期：{h1['last_date']}；持仓 {h1['n_held']} 只；权重和 {_num(h1['gross'])}
- 有仓日平均持仓只数 {_num(h1['mean_n'], 2)}（中位 {_num(h1['median_n'], 2)}）

{hold1_md}

同日 F0 持仓：`{', '.join(h0['codes'])}`（n={h0['n_held']}, gross={_num(h0['gross'])}）

## 6. 白话结论

1. `custom_max_inv` 是反彩票底部否决（拦新开），不是 E 式对称质量主确认；未使用反转/吸筹。
2. 相对同池 F0 的 Δexcess / Δsharpe 是迁移成败的第一判据。
3. VR/AR 产品周期、消费电子景气与主题炒作波动大，单板块结果易过拟合。
4. 若 excess<0 或相对 F0 无增量，应回退 F0，禁止为追收益擅自加反转/扩因子。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F1_RUN.resolve()}`。
"""

    (F0_RUN / "REPORT_F0_vrar.md").write_text(report_f0, encoding="utf-8")
    (F1_RUN / "REPORT_F1_vrar.md").write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            F0_RUN,
            "F0双确认-虚拟现实BK0722",
            "请用国泰君安191做F0双确认多因子选股并回测：A股虚拟现实/VR头显概念（东财BK0722）",
        ),
        (
            F1_RUN,
            "F1 MAX否决-虚拟现实BK0722",
            "请用国泰君安191+custom_max_inv做F1多因子选股并回测：A股虚拟现实/VR头显概念（东财BK0722）",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {
                        "panel": PANEL_LABEL,
                        "universe_run": SRC_RUN.name,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print("wrote reports + req.json")


def main() -> None:
    if not UNIVERSE_JSON.exists():
        raise SystemExit(
            f"missing {UNIVERSE_JSON}; dump BK0722 codes from browser first"
        )
    raw = json.loads(UNIVERSE_JSON.read_text(encoding="utf-8"))
    codes_raw = raw["codes"] if isinstance(raw, dict) and "codes" in raw else raw
    univ = _normalize_universe(codes_raw)
    listed_n = len(codes_raw)
    print(f"[universe] raw={listed_n} after_ex_BJ={len(univ)}")

    SRC_RUN.mkdir(parents=True, exist_ok=True)
    univ.to_csv(SRC_RUN / "universe.csv", index=False, encoding="utf-8-sig")
    (SRC_RUN / "universe_meta.json").write_text(
        json.dumps(
            {
                "panel": PANEL_LABEL,
                "source": "eastmoney BK0722",
                "listed_raw": listed_n,
                "ex_bj": len(univ),
                "codes": univ["ts_code"].tolist(),
                "names": {r.ts_code: r.name for r in univ.itertuples()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    data_map = fetch_ohlcv(univ["ts_code"].tolist(), SRC_RUN / "artifacts")
    if len(data_map) < 20:
        raise SystemExit(f"too few OHLCV: {len(data_map)}")
    name_map = {r.ts_code: r.name for r in univ.itertuples() if r.ts_code in data_map}
    codes = sorted(data_map.keys())
    (SRC_RUN / "config.json").write_text(
        json.dumps(
            {
                "codes": codes,
                "source": "tushare",
                "start_date": START_DATE,
                "end_date": END_DATE,
                "interval": "1D",
                "initial_cash": 1_000_000,
                "parameters": {"panel": PANEL_LABEL},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 60, "F0")
    _prepare(F0_RUN, codes, data_map, ENGINE_F0, "F0_vrar")
    f0 = _run(F0_RUN, data_map)
    print("F0 excess", f0["metrics"].get("excess_return"))

    print("=" * 60, "F1")
    _prepare(F1_RUN, codes, data_map, ENGINE_F1, "F1_vrar")
    f1 = _run(F1_RUN, data_map)
    print("F1 excess", f1["metrics"].get("excess_return"))

    _write_reports(f0, f1, name_map, listed_n)
    print("[done]", F0_RUN.name, F1_RUN.name)


if __name__ == "__main__":
    main()
