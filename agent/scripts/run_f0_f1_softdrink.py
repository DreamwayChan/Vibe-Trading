"""Build 软饮料 (东财行业「软饮料」BK1586) panel, run F0+F1, write comparison reports.

Universe: Eastmoney industry board BK1586 (软饮料) — A-share soft-drink core list.
BJ names dropped.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

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

SRC_RUN = _AGENT_ROOT / "runs" / "20260729_184000_00_softdrink_universe"
F0_RUN = _AGENT_ROOT / "runs" / "20260729_184000_01_F0_softdrink"
F1_RUN = _AGENT_ROOT / "runs" / "20260729_184000_02_F1_softdrink"
UNIVERSE_JSON = _AGENT_ROOT / "runs" / "_tmp_softdrink_universe" / "bk1586.json"
PROP_OUT = _AGENT_ROOT / "prop_国泰君安_软饮料_最优.md"
COMPARE_OUT = _AGENT_ROOT / "prop_国泰君安_F0_F1_软饮料对比.md"

START_DATE = "2024-01-01"
END_DATE = "2026-07-28"
MIN_BARS = 120
MC_SIMS = 1000
PANEL_LABEL = "软饮料 BK1586"
BK_CODE = "BK1586"
BK_NAME = "软饮料"


def fetch_universe() -> dict:
    """Refresh BK1586 constituents via push2delay (works when push2 is proxied)."""
    s = requests.Session()
    s.trust_env = False
    all_rows: list[dict] = []
    total = None
    for pn in range(1, 10):
        params = dict(
            pn=pn,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f12",
            fs=f"b:{BK_CODE}",
            fields="f12,f13,f14",
        )
        r = s.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        total = data.get("total", total)
        diff = data.get("diff") or []
        if not diff:
            break
        for x in diff:
            c = str(x.get("f12") or "").zfill(6)
            mkt = int(x.get("f13") or 0)
            if mkt == 1:
                ts = f"{c}.SH"
            elif c.startswith(("4", "8", "92")):
                ts = f"{c}.BJ"
            else:
                ts = f"{c}.SZ"
            all_rows.append({"ts_code": ts, "name": str(x.get("f14") or ""), "market": mkt})
        if total and len(all_rows) >= total:
            break
    payload = {
        "n": len(all_rows),
        "total": total,
        "bk": BK_CODE,
        "name": BK_NAME,
        "codes": all_rows,
    }
    UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[universe] {BK_CODE} n={len(all_rows)}")
    return payload


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
        if _is_bj(code) or code.split(".")[0].startswith("92"):
            continue
        rows.append({"ts_code": code, "name": name, "board": PANEL_LABEL})
    return (
        pd.DataFrame(rows)
        .drop_duplicates("ts_code")
        .sort_values("ts_code")
        .reset_index(drop=True)
    )


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
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_fetch_one_tushare, c, START_DATE, END_DATE): c for c in pending}
        for fut in as_completed(futs):
            code, df, err = fut.result()
            if df is None or len(df) < MIN_BARS:
                _, df2, err2 = _fetch_one_akshare(code, START_DATE, END_DATE)
                if df2 is None or len(df2) < MIN_BARS:
                    fail += 1
                    print(f"  skip {code}: {err or err2 or 'short'}")
                    continue
                df = df2
            out = df.copy()
            out.index.name = "trade_date"
            out.to_csv(art / f"ohlcv_{code}.csv")
            data_map[code] = out
            ok += 1
    print(f"[fetch] done total={len(data_map)} new_ok={ok} fail={fail} {time.time()-t0:.1f}s")
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
            "universe": "东财行业 软饮料 BK1586（A股软饮料股核心池，排除北交所）",
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


def _write_prop(winner: str, f0: dict, f1: dict) -> None:
    """Write a frontend/agent-ready prop prompt for the winning strategy."""
    m0, m1 = f0["metrics"], f1["metrics"]
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    if winner == "F1":
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为软饮料板块（东财行业「软饮料」BK1586）上的最优复现：F1 = F0 硬确认 + custom_max_inv 底部否决。
同池实测（run `{F1_RUN.name}` vs `{F0_RUN.name}`）：
- F1 相对 F0：Δtotal={_pct(d_total)}，Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}
- 结构依据仍来自 BK1184 bake-off（veto_max_20）；本版不重做因子挖掘。

本版只做一件事：在软饮料上复用 F0 + custom_max_inv 底部否决，不改过程约束。
优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认；若不及同池 F0 明文写「F1 未在软饮料带来增量」；若明显弱于板块基准明文写「F1 结构未在软饮料迁移成功」，禁止为追收益擅自加反转/扩因子。

【股票池与区间】
- 标的池：A股软饮料相关板块（东财行业「软饮料」BK1586，排除北交所）
- 研究/回测参考区间：{START_DATE} 至 {END_DATE}（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版仅 4 个）】
| ID | 角色 |
| gtja191_164 | 动量（主排序，必须） |
| gtja191_171 | 确认（F0 硬确认） |
| gtja191_054 | 确认（F0 硬确认） |
| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |

复制用：gtja191_164, gtja191_171, gtja191_054, custom_max_inv
【禁止】gtja191_114 / 反转因子；禁止用 custom 替换 171/054；
禁止 custom_ivol/abn_to/vp_corr/comp_low/near_low 进硬确认或重排；
禁止把 max_inv 做成「≥40% 分位才开仓」的对称质量过滤（那是 E，已证伪）

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权(171:0.155, 054:0.152)；缺谁跳谁并重归一
4. F0 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. F1 额外否决（仅新开仓）：smooth(custom_max_inv) < 当日截面 20% 分位 → 禁止新开
   - 已持仓续持不受该否决约束（只拦新进）
   - max 全缺日跳过否决
6. 面板只建 OHLCV；不要为 114 拼 amount

【持仓与换手（硬性）】
1. N=6 等权；保留带 Top9（N×1.5）
2. 调仓日：先过 F0 确认，再过 MAX 底部否决，按 S 选 Top6；旧仓仍在 Top9 且未触发风控可续持
3. 换手硬顶 20%：优先留旧仓，再填新股；将超 20% 则停止换入（空仓建仓首期除外）
4. 最小持有 5 日：未满 5 日不因跌出保留带而卖；峰值回撤止损/止盈立即生效
5. 择时：板块等权均价 vs MA20，仅调仓日更新乘数（1.0/0.5）；非调仓日冻结

【退出】
峰值回撤≥15% → 止盈≥35% → 持仓≥20日且排名恶化≥10名 → 调仓日跌出 Top9（且满 5 日）
非调仓日不因排名换股。

【交付】
- 因子 ID + 角色；说明 custom_max_inv 是「反彩票底部否决」不是质量主确认；未使用反转/吸筹
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照同池 F0：给出 Δtotal / Δexcess / Δsharpe；若 excess 未提升明文承认
- 3–5 条白话：为何否决不同于 E 硬过滤、相对基准、过拟合风险（含夏季消费/糖价包材/渠道景气周期）
- 声明：研究/回测，非实盘建议

```
"""
    else:
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为软饮料板块（东财行业「软饮料」BK1586）上的最优复现：F0 = 动量 164 + 硬确认 171/054。
同池实测 F1（MAX 底部否决）未带来增量（Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}），故默认回退 F0。
禁止为追收益叠加 custom 否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股软饮料相关板块（东财行业「软饮料」BK1586，排除北交所）
- 研究/回测参考区间：{START_DATE} 至 {END_DATE}（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版仅 3 个）】
| ID | 角色 |
| gtja191_164 | 动量（主排序，必须） |
| gtja191_171 | 确认（F0 硬确认） |
| gtja191_054 | 确认（F0 硬确认） |

复制用：gtja191_164, gtja191_171, gtja191_054
【禁止】gtja191_114 / 反转因子；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权(171:0.155, 054:0.152)；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 面板只建 OHLCV；不要为 114 拼 amount

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
- 因子 ID + 角色；未使用反转/吸筹/custom
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照同池 F1：给出 Δtotal / Δexcess / Δsharpe；说明为何以 F0 为准
- 3–5 条白话：相对基准、牛熊市/成交额周期/过拟合风险
- 声明：研究/回测，非实盘建议

```
"""
    PROP_OUT.write_text(body, encoding="utf-8")
    print(f"wrote prop {PROP_OUT} (winner={winner})")


def _write_reports(f0: dict, f1: dict, name_map: dict[str, str], listed_n: int) -> str:
    m0, m1 = f0["metrics"], f1["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(F0_RUN, name_map)
    hold1_md, h1 = _holdings(F1_RUN, name_map)
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])
    winner = "F1" if d_excess > 0 else "F0"

    eq = pd.read_csv(F0_RUN / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    # ---- standalone comparison report (primary deliverable) ----
    compare = f"""# F0 vs F1 对比报告 — A股软饮料（软饮料 BK1586）

**声明：研究/回测结果，非实盘建议。**

## 1. 研究设定

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（A股软饮料） |
| 成分 | 东财行业 BK1586；排除北交所；原始 {listed_n} → 有效 OHLCV {f0['n_symbols']} |
| 区间 | 配置 {START_DATE} → {END_DATE}；实际 {actual_start} → {actual_end} |
| F0 | 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054` |
| F1 | F0 + `custom_max_inv` 底部 20% 否决新开仓 |
| F0 run | `{F0_RUN.name}` |
| F1 run | `{F1_RUN.name}` |
| 判据 | 主看 **excess_return**；辅看 sharpe / max_dd / MC p |

## 2. 核心对比表

| 指标 | F0 | F1 | F1−F0 |
|------|----|----|-------|
| total_return | {_pct(m0['total_return'])} | {_pct(m1['total_return'])} | {_pct(d_total)} |
| annual_return | {_pct(m0['annual_return'])} | {_pct(m1['annual_return'])} | — |
| benchmark_return | {_pct(m0['benchmark_return'])} | {_pct(m1['benchmark_return'])} | 0.00% |
| **excess_return** | {_pct(m0['excess_return'])} | {_pct(m1['excess_return'])} | {_pct(d_excess)} |
| sharpe | {_num(m0['sharpe'])} | {_num(m1['sharpe'])} | {_num(d_sharpe)} |
| max_drawdown | {_pct(m0['max_drawdown'])} | {_pct(m1['max_drawdown'])} | — |
| win_rate | {_pct(m0['win_rate'])} | {_pct(m1['win_rate'])} | — |
| profit_factor | {_num(m0['profit_factor'])} | {_num(m1['profit_factor'])} | — |
| trade_count | {_num(m0.get('trade_count'), 0)} | {_num(m1.get('trade_count'), 0)} | — |
| rebalance_count | {_num(m0.get('rebalance_count'), 0)} | {_num(m1.get('rebalance_count'), 0)} | — |
| avg_holding_days | {_num(m0.get('avg_holding_days'))} | {_num(m1.get('avg_holding_days'))} | — |
| rebalance_turnover_mean | {_num(m0.get('rebalance_turnover_mean'))} | {_num(m1.get('rebalance_turnover_mean'))} | — |
| is_rebalance_days | {f0.get('is_rebalance_days')} | {f1.get('is_rebalance_days')} | — |
| MC p_sharpe | {_num(v0.get('p_value_sharpe'))} | {_num(v1.get('p_value_sharpe'))} | — |
| final_value | {_num(m0['final_value'], 2)} | {_num(m1['final_value'], 2)} | — |

## 3. 结论

"""
    if d_excess > 0:
        compare += (
            f"> **本板块最优为 F1**。"
            f"相对 F0：Δtotal={_pct(d_total)}，Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}。\n\n"
        )
    else:
        compare += (
            f"> **本板块以 F0 为准**；F1（MAX 底部否决）未带来增量"
            f"（Δtotal={_pct(d_total)}，Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}）。\n\n"
        )

    if float(m0["excess_return"]) < 0 and float(m1["excess_return"]) < 0:
        compare += (
            f"- 两策略 excess 均为负（F0 {_pct(m0['excess_return'])} / "
            f"F1 {_pct(m1['excess_return'])}），相对软饮料等权基准均未跑赢。\n"
        )
    elif float(m0["excess_return"]) < 0:
        compare += f"- F0 excess = {_pct(m0['excess_return'])} < 0。\n"
    elif float(m1["excess_return"]) < 0:
        compare += f"- F1 excess = {_pct(m1['excess_return'])} < 0。\n"

    for label, v in (("F0", v0), ("F1", v1)):
        p = v.get("p_value_sharpe")
        if p is not None and float(p) > 0.2:
            compare += f"- {label} MC p_sharpe = {_num(p)} > 0.2，统计不显著。\n"
        else:
            compare += f"- {label} MC p_sharpe = {_num(p)}（阈值 0.2）。\n"

    compare += f"""
## 4. 最近持仓对照

### F0（{h0['last_date']}，{h0['n_held']} 只）

{hold0_md}

### F1（{h1['last_date']}，{h1['n_held']} 只）

{hold1_md}

## 5. 白话解读

1. F0 = 纯动量 + 171/054 硬确认；F1 仅在新开仓叠加 `custom_max_inv` 底部 20% 否决，不改周频/换手帽/最小持有。
2. 软饮料股受夏季消费、糖价/包材成本与渠道景气驱动，单板块结果易随市场周期过拟合。
3. 主判据是相对同池的 Δexcess；若 F1 无增量应回退 F0，禁止为追收益擅自加反转/扩因子。
4. 成分取自东财行业「软饮料」BK1586（软饮料/饮料核心池），非松散概念合集。
5. 研究/回测，非实盘建议。

---

Artifacts：`{F0_RUN.resolve()}` / `{F1_RUN.resolve()}`
"""
    COMPARE_OUT.write_text(compare, encoding="utf-8")
    (F0_RUN / "REPORT_COMPARE_softdrink.md").write_text(compare, encoding="utf-8")
    (F1_RUN / "REPORT_COMPARE_softdrink.md").write_text(compare, encoding="utf-8")

    report_f0 = f"""# F0 双确认报告 — 软饮料 BK1586

**声明：研究/回测结果，非实盘建议。本报告为软饮料板块（东财行业「软饮料」BK1586）的 F0（164 + 171/054 硬确认）正式复跑。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（A股软饮料） |
| 策略 | F0 = 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054`（无 custom 否决） |
| F0 run | `{F0_RUN.name}` |
| 同池 F1 对照 | `{F1_RUN.name}` |
| 成分来源 | 东财行业 BK1586；排除北交所；原始 {listed_n} → 有效 OHLCV {f0['n_symbols']} |
| 配置区间 | {START_DATE} → {END_DATE} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `gtja191_164, gtja191_171, gtja191_054` |
| is_rebalance_days | {f0.get('is_rebalance_days')} |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_171 | F0 硬确认（IR 0.155） |
| gtja191_054 | F0 硬确认（IR 0.152） |

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
        report_f0 += f"> **对照：同池 F1 excess 高于 F0**（Δexcess={_pct(d_excess)}）。详见 F1 报告；本板块最优为 F1。\n"
    else:
        report_f0 += f"> **结论：本板块以 F0 为准**；F1 未带来增量（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。\n"

    report_f0 += f"""
## 5. 最近调仓日持仓（F0）

- 日期：{h0['last_date']}；持仓 {h0['n_held']} 只；权重和 {_num(h0['gross'])}
- 有仓日平均持仓只数 {_num(h0['mean_n'], 2)}（中位 {_num(h0['median_n'], 2)}）

{hold0_md}

## 6. 白话结论

1. F0 = 纯动量 + 171/054 硬确认；周频 / 换手帽 / 最小持有 / 择时写死在 signal_engine。
2. 软饮料受夏季消费、糖价/包材与渠道景气影响，单板块结果易随市场周期过拟合。
3. 成分取自东财行业「软饮料」BK1586（软饮料/饮料核心标的）。
4. 相对同池 F1 的 Δexcess 决定是否叠加 MAX 否决。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F0_RUN.resolve()}`。
"""

    report_f1 = f"""# F1 MAX否决报告 — 软饮料 BK1586

**声明：研究/回测结果，非实盘建议。F1 = F0 + `custom_max_inv` 底部 20% 否决；迁移至软饮料板块（东财 BK1586）。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（A股软饮料） |
| 策略 | F1 = F0(164+171/054) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1 run | `{F1_RUN.name}` |
| 同池 F0 对照 | `{F0_RUN.name}` |
| 成分来源 | 东财行业 BK1586；排除北交所；有效 OHLCV {f1['n_symbols']} |
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
        report_f1 += "- **F1 结构未在软饮料迁移成功**（相对板块基准为负）。\n"
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
            f"- **F1 未在软饮料带来增量**"
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
            f"（Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。**本板块最优为 F1。**\n"
        )
    else:
        report_f1 += (
            f"> **同板块结论：F1 未在软饮料带来增量**"
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
3. 软饮料受夏季消费、糖价/包材成本与渠道景气驱动，主题波动大，单板块易过拟合。
4. 若 excess<0 或相对 F0 无增量，应回退 F0，禁止为追收益擅自加反转/扩因子。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F1_RUN.resolve()}`。
"""

    (F0_RUN / "REPORT_F0_softdrink.md").write_text(report_f0, encoding="utf-8")
    (F1_RUN / "REPORT_F1_softdrink.md").write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            F0_RUN,
            "F0双确认-软饮料BK1586",
            "请用国泰君安191做F0双确认多因子选股并回测：A股软饮料（东财行业软饮料 BK1586）",
        ),
        (
            F1_RUN,
            "F1 MAX否决-软饮料BK1586",
            "请用国泰君安191+custom_max_inv做F1多因子选股并回测：A股软饮料（东财行业软饮料 BK1586）",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {"panel": PANEL_LABEL, "universe_run": SRC_RUN.name},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    _write_prop(winner, f0, f1)
    print("wrote reports + compare + req.json; winner=", winner)
    return winner


def main() -> None:
    payload = fetch_universe()
    univ = _normalize_universe(payload["codes"])
    listed_n = len(payload["codes"])
    print(f"[universe] raw={listed_n} after_ex_BJ={len(univ)}")
    # Soft-drink industry board is small (~8 names); N=6 still needs a viable core.
    if len(univ) < 6:
        raise SystemExit(f"universe too small: {len(univ)}")

    SRC_RUN.mkdir(parents=True, exist_ok=True)
    univ.to_csv(SRC_RUN / "universe.csv", index=False, encoding="utf-8-sig")
    (SRC_RUN / "universe_meta.json").write_text(
        json.dumps(
            {
                "panel": PANEL_LABEL,
                "source": f"eastmoney industry {BK_CODE}",
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
    if len(data_map) < 6:
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
    _prepare(F0_RUN, codes, data_map, ENGINE_F0, "F0_softdrink")
    f0 = _run(F0_RUN, data_map)
    print("F0 excess", f0["metrics"].get("excess_return"))

    print("=" * 60, "F1")
    _prepare(F1_RUN, codes, data_map, ENGINE_F1, "F1_softdrink")
    f1 = _run(F1_RUN, data_map)
    print("F1 excess", f1["metrics"].get("excess_return"))

    winner = _write_reports(f0, f1, name_map, listed_n)
    print("[done]", F0_RUN.name, F1_RUN.name, "winner", winner)
    print("[compare]", COMPARE_OUT)


if __name__ == "__main__":
    main()
