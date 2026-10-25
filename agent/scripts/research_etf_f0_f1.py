"""Screen all A-share listed ETFs with F0 / F1 and pick Top15.

Pipeline
--------
1. List exchange ETFs via AkShare Sina category (Tushare fund/etf APIs need
   higher points than this workspace token has).
2. Keep equity-like names; drop bond / money / commodity / REIT-style products.
3. Take the most liquid names by spot turnover, download OHLCV, require enough
   history for gtja191 windows.
4. Run F0 (164 + 171/054 hard confirm) and F1 (F0 + custom_max_inv bottom-20%
   veto) with N=15 / band=22 on the same panel.
5. Write run artifacts + REPORT_ETF_F0_F1.md under runs/.

声明：研究/回测，非实盘建议。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.loaders._symbol_utils import _is_etf_listed  # noqa: E402
from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import (  # noqa: E402
    _AutoLoader,
    _create_market_engine,
    _load_module_from_file,
)

# Canonical engines from the BK1184 bake-off lineage.
ENGINE_F0_SRC = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "F0" / "code" / "signal_engine.py"
)
ENGINE_F1_SRC = (
    _AGENT_ROOT
    / "runs"
    / "_research_custom_positive"
    / "veto_max_20"
    / "code"
    / "signal_engine.py"
)

RUN_ROOT = _AGENT_ROOT / "runs" / "20260726_113500_01_ETF_F0F1"
CACHE_DIR = RUN_ROOT / "ohlcv_cache"
F0_RUN = RUN_ROOT / "F0"
F1_RUN = RUN_ROOT / "F1"
META_PATH = RUN_ROOT / "universe_meta.json"
REPORT_PATH = RUN_ROOT / "REPORT_ETF_F0_F1.md"

START_DATE = "2024-01-01"
END_DATE = "2026-07-24"
# Cross-section size: liquid enough for factor CS%, small enough to fetch.
UNIVERSE_CAP = 180
MIN_BARS = 180
TOP_N = 15
BAND = 22  # N * 1.5
MAX_WORKERS = 12
MC_SIMS = 500

# Drop non-equity products — F0/F1 factors are equity OHLCV momentum/confirm.
# Non-capturing group — pandas str.contains treats () as regex groups otherwise.
_EXCLUDE_NAME_RE = re.compile(
    r"(?:债|货币|日利|添益|现金|理财|黄金|白银|豆粕|原油|能源化工|"
    r"有色期货|商品|REIT|reits|房地产信托|国债|政金债|信用债|公司债|"
    r"短融|超短|可转债|转债|同业存单|城投)",
    re.IGNORECASE,
)


def _sina_to_ts(code: str) -> str | None:
    """sz159915 / sh510300 -> 159915.SZ / 510300.SH."""
    raw = str(code).strip().lower()
    if len(raw) < 8:
        return None
    exch, digits = raw[:2], raw[2:]
    if exch == "sh":
        return f"{digits}.SH"
    if exch == "sz":
        return f"{digits}.SZ"
    return None


def list_equity_etfs(cap: int = UNIVERSE_CAP) -> pd.DataFrame:
    """List A-share ETFs and keep liquid equity-like names."""
    import akshare as ak

    spot = ak.fund_etf_category_sina(symbol="ETF基金")
    frame = spot.rename(
        columns={
            "代码": "sina_code",
            "名称": "name",
            "成交额": "amount",
            "成交量": "volume",
            "最新价": "last",
        }
    ).copy()
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame["ts_code"] = frame["sina_code"].map(_sina_to_ts)
    frame = frame.dropna(subset=["ts_code", "amount"])
    frame = frame[frame["ts_code"].map(_is_etf_listed)]
    frame["name"] = frame["name"].astype(str)
    # Equity-like filter: drop bond/money/commodity by Chinese name keywords.
    keep = ~frame["name"].str.contains(_EXCLUDE_NAME_RE, na=False)
    equity = frame.loc[keep].copy()
    # Deduplicate identical ts_code (rare), keep highest spot amount.
    equity = (
        equity.sort_values("amount", ascending=False)
        .drop_duplicates("ts_code", keep="first")
        .reset_index(drop=True)
    )
    selected = equity.head(int(cap)).copy()
    selected["rank_by_amount"] = np.arange(1, len(selected) + 1)
    return selected, equity


def _fetch_one(ts_code: str, start: str, end: str) -> tuple[str, pd.DataFrame | None, str]:
    """Fetch one ETF OHLCV via Sina; return (code, df|None, err)."""
    import akshare as ak

    digits, _, suffix = ts_code.upper().partition(".")
    symbol = f"{suffix.lower()}{digits}"
    try:
        raw = ak.fund_etf_hist_sina(symbol=symbol)
    except Exception as exc:  # network / empty
        return ts_code, None, str(exc)
    if raw is None or raw.empty:
        return ts_code, None, "empty"
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep].dropna(subset=["open", "high", "low", "close"])
    df = df.loc[start:end]
    if df.empty:
        return ts_code, None, "out_of_range"
    return ts_code, df, ""


def fetch_universe(
    meta: pd.DataFrame,
    cache_dir: Path,
    start: str,
    end: str,
    min_bars: int = MIN_BARS,
    max_workers: int = MAX_WORKERS,
) -> dict[str, pd.DataFrame]:
    """Download (or reload) OHLCV for the candidate universe."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    pending: list[str] = []

    for ts_code in meta["ts_code"].tolist():
        path = cache_dir / f"ohlcv_{ts_code}.csv"
        if path.exists():
            df = pd.read_csv(path, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
            df = df.loc[start:end]
            if len(df) >= min_bars:
                data_map[ts_code] = df
                continue
        pending.append(ts_code)

    print(f"[fetch] cached_ok={len(data_map)} pending={len(pending)}")
    if not pending:
        return data_map

    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_fetch_one, c, start, end): c for c in pending}
        for fut in as_completed(futs):
            code, df, err = fut.result()
            if df is None or len(df) < min_bars:
                fail += 1
                if fail <= 8:
                    print(f"  skip {code}: {err or f'bars={0 if df is None else len(df)}'}")
                continue
            out = df.copy()
            out.index.name = "trade_date"
            out.to_csv(cache_dir / f"ohlcv_{code}.csv")
            data_map[code] = out
            ok += 1
            if ok % 20 == 0:
                print(f"  fetched {ok}/{len(pending)} (+{fail} fail) {time.time()-t0:.1f}s")
    print(f"[fetch] done ok={ok} fail={fail} total={len(data_map)} in {time.time()-t0:.1f}s")
    return data_map


def _patch_engine(src: Path, dst: Path, top_n: int, band: int) -> None:
    """Copy engine and pin TopN / retain-band for the ETF screen."""
    text = src.read_text(encoding="utf-8")
    text = re.sub(r"self\.top_n\s*=\s*\d+", f"self.top_n = {int(top_n)}", text, count=1)
    text = re.sub(r"self\.band\s*=\s*\d+", f"self.band = {int(band)}", text, count=1)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


def _prepare_run(
    run_dir: Path,
    codes: list[str],
    data_map: dict[str, pd.DataFrame],
    engine_src: Path,
    strategy: str,
    name_map: dict[str, str],
) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    art = run_dir / "artifacts"
    art.mkdir(parents=True)
    for code in codes:
        df = data_map[code]
        out = df.copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")
    _patch_engine(engine_src, run_dir / "code" / "signal_engine.py", TOP_N, BAND)
    alphas = ["gtja191_164", "gtja191_171", "gtja191_054"]
    if "F1" in strategy:
        alphas = alphas + ["custom_max_inv"]
    cfg = {
        "codes": codes,
        "source": "akshare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": 1_000_000,
        "commission": 0.0003,
        "stamp_duty": 0.0,
        "slippage": 0.0005,
        "alphas": alphas,
        "parameters": {
            "strategy": strategy,
            "panel": "A股权益ETF流动性截面",
            "engine": "F1_veto_max20" if "F1" in strategy else "F0_dual",
            "top_n": TOP_N,
            "band": BAND,
            "universe_cap": UNIVERSE_CAP,
            "name_map_n": len(name_map),
        },
        "validation": {"monte_carlo": {"n_simulations": MC_SIMS}},
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "name_map.json").write_text(
        json.dumps(name_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run_backtest(run_dir: Path, data_map: dict[str, pd.DataFrame]) -> dict:
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    codes = [c for c in cfg["codes"] if c in data_map]
    subset = {c: data_map[c] for c in codes}
    print(f"[{run_dir.name}] symbols={len(subset)}")

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"eng_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    cfg["codes"] = list(subset.keys())
    cfg["source"] = "akshare"
    cfg["_run_card_effective_sources"] = ["local_artifacts"]

    engine = _create_market_engine("akshare", cfg, cfg["codes"])
    bpy = calc_bars_per_year(cfg.get("interval", "1D"), "akshare")
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


def _holdings_table(run_dir: Path, name_map: dict[str, str]) -> tuple[str, dict]:
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
        "codes": list(held.index),
        "weights": {c: float(w) for c, w in held.items()},
    }
    lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
    for i, (code, w) in enumerate(held.items(), 1):
        lines.append(f"| {i} | {code} | {name_map.get(code, '')} | {float(w):.4f} |")
    return "\n".join(lines), stats


def _eligible_top15(
    run_dir: Path,
    data_map: dict[str, pd.DataFrame],
    name_map: dict[str, str],
    strategy: str,
) -> tuple[str, pd.DataFrame]:
    """Rank names that pass strategy filters on the last rebalance day (Top15)."""
    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"elig_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    # Reuse generate internals: build panel + scores, then rank.
    panel = sig._build_panel(data_map)
    close = panel["close"]
    pack = sig._compute_scores(panel, close)
    if pack is None:
        return "_compute_scores failed_", pd.DataFrame()

    if strategy.startswith("F1"):
        sort_score, confirm_ok, _custom = pack
    else:
        # F0 engine returns (sort_score, confirm_ok)
        if len(pack) == 2:
            sort_score, confirm_ok = pack
        else:
            sort_score, confirm_ok = pack[0], pack[1]

    dates = close.index
    is_reb = sig._rebalance_mask(dates)
    reb_idx = np.where(np.asarray(is_reb))[0]
    if len(reb_idx) == 0:
        return "no rebalance day", pd.DataFrame()
    i = int(reb_idx[-1])
    day = dates[i]
    ok = confirm_ok.iloc[i]
    score = sort_score.iloc[i]
    rank = score.rank(ascending=False, method="first")
    rows = []
    for code in score.index:
        if not bool(ok.get(code, False)):
            continue
        if pd.isna(score.get(code)):
            continue
        rows.append(
            {
                "ts_code": code,
                "name": name_map.get(code, ""),
                "S": float(score[code]),
                "rank": float(rank[code]),
                "pass_filter": True,
            }
        )
    elig = pd.DataFrame(rows).sort_values("rank").head(TOP_N).reset_index(drop=True)
    elig.insert(0, "#", np.arange(1, len(elig) + 1))
    elig["trade_date"] = str(pd.Timestamp(day).date())
    lines = [
        f"- 截面日（最近调仓日）：{elig['trade_date'].iloc[0] if len(elig) else 'n/a'}",
        f"- 通过过滤且按 S 排序的 Top{TOP_N}：",
        "",
        "| # | 代码 | 名称 | S分位平滑 | 动量排名 |",
        "|---|------|------|-----------|----------|",
    ]
    for _, r in elig.iterrows():
        lines.append(
            f"| {int(r['#'])} | {r['ts_code']} | {r['name']} | {r['S']:.4f} | {int(r['rank'])} |"
        )
    return "\n".join(lines), elig


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


def _write_report(
    listed_n: int,
    equity_n: int,
    universe: pd.DataFrame,
    f0: dict,
    f1: dict,
    hold0_md: str,
    hold1_md: str,
    h0: dict,
    h1: dict,
    elig0_md: str,
    elig1_md: str,
    elig0: pd.DataFrame,
    elig1: pd.DataFrame,
) -> None:
    m0, m1 = f0["metrics"], f1["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    # Prefer the strategy with higher excess; tie-break sharpe.
    prefer_f1 = (d_excess > 0) or (d_excess == 0 and d_sharpe > 0)
    winner = "F1" if prefer_f1 else "F0"
    pick = elig1 if prefer_f1 else elig0
    pick_md = elig1_md if prefer_f1 else elig0_md

    lines = [
        "# A股 ETF × F0/F1 筛选报告",
        "",
        "**声明：研究/回测结果，非实盘建议。**",
        "",
        "## 1. 运行概况",
        "",
        "| 项 | 内容 |",
        "|----|------|",
        f"| 上市 ETF 全量（Sina） | {listed_n} |",
        f"| 剔除债/货/商品等后 | {equity_n} |",
        f"| 流动性截取候选 | {len(universe)}（按现货成交额 Top{UNIVERSE_CAP}） |",
        f"| 有效 OHLCV（≥{MIN_BARS} 根） | {f0['n_symbols']} |",
        f"| 区间 | {START_DATE} → {END_DATE} |",
        f"| 策略设定 | TopN={TOP_N}，保留带={BAND}，周频，换手帽 20%，确认 40%，F1 否决 20% |",
        f"| F0 run | `{F0_RUN.relative_to(_AGENT_ROOT)}` |",
        f"| F1 run | `{F1_RUN.relative_to(_AGENT_ROOT)}` |",
        f"| 数据源 | AkShare `fund_etf_category_sina` + `fund_etf_hist_sina`（Tushare ETF 接口无权限） |",
        "",
        "## 2. 因子角色（与个股版一致）",
        "",
        "| ID | 角色 |",
        "|----|------|",
        "| gtja191_164 | 动量主排序 S |",
        "| gtja191_171 | F0 硬确认（IR 0.155） |",
        "| gtja191_054 | F0 硬确认（IR 0.152） |",
        "| custom_max_inv | 仅 F1：反彩票底部 20% 否决（拦新开） |",
        "",
        "## 3. F0 vs F1 绩效（同 ETF 池）",
        "",
        "| 指标 | F0 | F1 | F1−F0 |",
        "|------|----|----|-------|",
        f"| total_return | {_pct(m0['total_return'])} | {_pct(m1['total_return'])} | {_pct(d_total)} |",
        f"| benchmark_return | {_pct(m0['benchmark_return'])} | {_pct(m1['benchmark_return'])} | — |",
        f"| excess_return | {_pct(m0['excess_return'])} | {_pct(m1['excess_return'])} | {_pct(d_excess)} |",
        f"| sharpe | {_num(m0['sharpe'])} | {_num(m1['sharpe'])} | {_num(d_sharpe)} |",
        f"| max_drawdown | {_pct(m0['max_drawdown'])} | {_pct(m1['max_drawdown'])} | — |",
        f"| trade_count | {_num(m0.get('trade_count'), 0)} | {_num(m1.get('trade_count'), 0)} | — |",
        f"| rebalance_turnover_mean | {_num(m0.get('rebalance_turnover_mean'))} | {_num(m1.get('rebalance_turnover_mean'))} | — |",
        f"| is_rebalance_days | {f0.get('is_rebalance_days')} | {f1.get('is_rebalance_days')} | — |",
        f"| MC p_sharpe | {_num(v0.get('p_value_sharpe'))} | {_num(v1.get('p_value_sharpe'))} | — |",
        f"| MC n_sim | {v0.get('n_simulations', MC_SIMS)} | {v1.get('n_simulations', MC_SIMS)} | — |",
        "",
        f"> **推荐策略：{winner}**"
        + (
            f"（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。"
            if prefer_f1
            else f"（F1 未带来增量：Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。"
        ),
        "",
        "### 必须承认",
        "",
    ]
    for label, m, v in (("F0", m0, v0), ("F1", m1, v1)):
        ex = float(m["excess_return"])
        p = v.get("p_value_sharpe")
        note = []
        if ex < 0:
            note.append(f"excess={_pct(ex)} < 0")
        if p is not None and float(p) > 0.2:
            note.append(f"MC p_sharpe={_num(p)} > 0.2")
        if not note:
            note.append(f"excess={_pct(ex)}，MC p_sharpe={_num(p)}（阈值 0.2）")
        lines.append(f"- {label}：{'；'.join(note)}")

    lines += [
        "",
        f"## 4. 推荐名单：{winner} 过滤后 Top{TOP_N}",
        "",
        pick_md,
        "",
        "## 5. 最近调仓日实际持仓（含换手帽/续持约束后）",
        "",
        f"### F0（{h0['last_date']}，持仓 {h0['n_held']}，权重和 {_num(h0['gross'])}）",
        "",
        hold0_md,
        "",
        f"### F1（{h1['last_date']}，持仓 {h1['n_held']}，权重和 {_num(h1['gross'])}）",
        "",
        hold1_md,
        "",
        "## 6. F0 过滤截面 Top15（对照）",
        "",
        elig0_md,
        "",
        "## 7. F1 过滤截面 Top15（对照）",
        "",
        elig1_md,
        "",
        "## 8. 白话结论",
        "",
        "1. 本任务把「A股上市权益 ETF」当成截面池，复用个股版 F0/F1 过程约束（周频 / 确认 40% / MAX 否决 / 换手帽）。",
        "2. 债基、货基、商品与 REIT 类产品已按名称剔除；最终截面是成交额靠前的权益 ETF，避免无流动性噪音主导分位。",
        "3. 推荐名单取 excess 更优策略在**最近调仓日**通过过滤后的动量 Top15；与「换手帽约束后的实际持仓」可能不完全相同。",
        "4. ETF 截面比主题个股更同质，因子区分度可能偏弱，样本外与费率/折溢价未计入。",
        "5. 研究/回测，非实盘建议。",
        "",
        "---",
        "",
        f"Artifacts: `{RUN_ROOT}`",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    # Machine-readable pick list
    pick_path = RUN_ROOT / f"TOP15_{winner}.csv"
    pick.to_csv(pick_path, index=False, encoding="utf-8-sig")
    print(f"[report] {REPORT_PATH}")
    print(f"[report] pick -> {pick_path}")


def main() -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    print("[1] list ETFs …")
    selected, equity_all = list_equity_etfs(UNIVERSE_CAP)
    listed_n = int(selected.attrs.get("listed_n", 0)) if hasattr(selected, "attrs") else 0
    # Re-count full sina list for the report.
    import akshare as ak

    listed_n = len(ak.fund_etf_category_sina(symbol="ETF基金"))
    equity_n = len(equity_all)
    print(f"  listed={listed_n} equity_like={equity_n} selected={len(selected)}")
    selected.to_csv(RUN_ROOT / "universe_candidates.csv", index=False, encoding="utf-8-sig")

    print("[2] fetch OHLCV …")
    data_map = fetch_universe(selected, CACHE_DIR, START_DATE, END_DATE)
    if len(data_map) < TOP_N * 2:
        raise SystemExit(f"too few ETFs with history: {len(data_map)}")

    name_map = {
        r.ts_code: r.name
        for r in selected.itertuples()
        if r.ts_code in data_map
    }
    codes = sorted(data_map.keys())
    META_PATH.write_text(
        json.dumps(
            {
                "listed_n": listed_n,
                "equity_n": equity_n,
                "selected_n": len(selected),
                "effective_n": len(codes),
                "start": START_DATE,
                "end": END_DATE,
                "top_n": TOP_N,
                "band": BAND,
                "codes": codes,
                "names": name_map,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("[3] prepare + run F0 …")
    _prepare_run(F0_RUN, codes, data_map, ENGINE_F0_SRC, "F0_etf", name_map)
    f0 = _run_backtest(F0_RUN, data_map)

    print("[4] prepare + run F1 …")
    _prepare_run(F1_RUN, codes, data_map, ENGINE_F1_SRC, "F1_etf", name_map)
    f1 = _run_backtest(F1_RUN, data_map)

    hold0_md, h0 = _holdings_table(F0_RUN, name_map)
    hold1_md, h1 = _holdings_table(F1_RUN, name_map)
    elig0_md, elig0 = _eligible_top15(F0_RUN, data_map, name_map, "F0")
    elig1_md, elig1 = _eligible_top15(F1_RUN, data_map, name_map, "F1")

    univ_eff = selected[selected["ts_code"].isin(codes)].copy()
    _write_report(
        listed_n,
        equity_n,
        univ_eff,
        f0,
        f1,
        hold0_md,
        hold1_md,
        h0,
        h1,
        elig0_md,
        elig1_md,
        elig0,
        elig1,
    )
    print("[done]")


if __name__ == "__main__":
    main()
