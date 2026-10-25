"""Run F0 vs F1 on three high-sustainability tech themes (from 2026-07-31 market review).

Sectors:
  1. 存储芯片         BK1137
  2. 半导体设备材料   BK1326 ∪ BK1325
  3. 算力硬件         BK1136 ∪ BK1340 ∪ BK1138  (光通信模块 ∪ PCB ∪ 液冷)

Same engines / constraints as pickable5 / snack runners.
Research only — not investment advice.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

START_DATE = "2024-01-01"
END_DATE = "2026-07-31"
MIN_BARS = 120
MC_SIMS = 1000
MAX_UNIVERSE = 80
RUN_PREFIX = "20260731_tech3"
SUMMARY_OUT = _AGENT_ROOT / "prop_国泰君安_F0_F1_tech3高可持续汇总.md"


@dataclass(frozen=True)
class Sector:
    slug: str
    name: str
    bks: tuple[str, ...]
    bk_names: dict[str, str]
    risk_note: str

    @property
    def bk_label(self) -> str:
        """Compact board label for reports (single code or union)."""
        if len(self.bks) == 1:
            return self.bks[0]
        return "∪".join(self.bks)

    @property
    def panel(self) -> str:
        return f"{self.name} {self.bk_label}"

    @property
    def universe_desc(self) -> str:
        parts = [f"{self.bk_names.get(b, b)} {b}" for b in self.bks]
        joined = " ∪ ".join(parts)
        return f"东财 {joined}（排除北交所；市值优先最多{MAX_UNIVERSE}只）"


SECTORS: list[Sector] = [
    Sector(
        slug="memory",
        name="存储芯片",
        bks=("BK1137",),
        bk_names={"BK1137": "存储芯片"},
        risk_note="存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动",
    ),
    Sector(
        slug="semeq",
        name="半导体设备材料",
        bks=("BK1326", "BK1325"),
        bk_names={"BK1326": "半导体设备", "BK1325": "半导体材料"},
        risk_note="资本开支节奏、国产替代进度与出口管制",
    ),
    Sector(
        slug="compute",
        name="算力硬件",
        bks=("BK1136", "BK1340", "BK1138"),
        bk_names={
            "BK1136": "光通信模块",
            "BK1340": "印制电路板",
            "BK1138": "液冷概念",
        },
        risk_note="云厂 CapEx、光模块/PCB/液冷订单兑现与估值拥挤",
    ),
]


def _paths(sec: Sector) -> dict[str, Path]:
    return {
        "src": _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_00_{sec.slug}_universe",
        "f0": _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_01_F0_{sec.slug}",
        "f1": _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_02_F1_{sec.slug}",
        "univ_json": _AGENT_ROOT / "runs" / f"_tmp_{sec.slug}_universe" / "universe.json",
        "prop": _AGENT_ROOT / f"prop_国泰君安_{sec.name}_最优.md",
        "compare": _AGENT_ROOT / f"prop_国泰君安_F0_F1_{sec.name}对比.md",
    }


def _fetch_bk(s: requests.Session, bk: str) -> list[dict]:
    """Pull one Eastmoney board, ordered by market cap (largest first)."""
    all_rows: list[dict] = []
    total = None
    for pn in range(1, 12):
        params = dict(
            pn=pn,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f20",
            fs=f"b:{bk}",
            fields="f12,f13,f14,f20",
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
            all_rows.append(
                {
                    "ts_code": ts,
                    "name": str(x.get("f14") or ""),
                    "market": mkt,
                    "mv": x.get("f20"),
                    "bk": bk,
                }
            )
        if total and len(all_rows) >= int(total):
            break
        time.sleep(0.08)
    print(f"[universe] {bk} n={len(all_rows)} total={total}")
    return all_rows


def fetch_universe(sec: Sector, univ_json: Path) -> dict:
    """Merge one or more boards; keep first (largest-cap) occurrence per code."""
    s = requests.Session()
    s.trust_env = False
    seen: dict[str, dict] = {}
    by_bk: dict[str, int] = {}
    # Iterate boards in order; within each board rows are already market-cap sorted.
    # Across boards we re-sort by mv after merge so the final cap prefers liquid names.
    for bk in sec.bks:
        rows = _fetch_bk(s, bk)
        by_bk[bk] = len(rows)
        for row in rows:
            code = row["ts_code"]
            if code not in seen:
                seen[code] = row
            else:
                # Prefer higher market-cap if the same name appears on multiple boards.
                prev_mv = seen[code].get("mv")
                new_mv = row.get("mv")
                try:
                    if new_mv is not None and (
                        prev_mv is None or float(new_mv) > float(prev_mv)
                    ):
                        seen[code] = row
                except (TypeError, ValueError):
                    pass

    all_rows = list(seen.values())

    def _mv_key(item: dict) -> float:
        try:
            return float(item.get("mv") or 0)
        except (TypeError, ValueError):
            return 0.0

    all_rows.sort(key=_mv_key, reverse=True)
    payload = {
        "n": len(all_rows),
        "bk": "+".join(sec.bks),
        "name": sec.name,
        "by_bk": by_bk,
        "codes": all_rows,
    }
    univ_json.parent.mkdir(parents=True, exist_ok=True)
    univ_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[universe] {sec.name} merged n={len(all_rows)} by_bk={by_bk}")
    return payload


def _is_bj(code: str) -> bool:
    digits = code.split(".")[0]
    return digits.startswith(("4", "8", "92")) or code.upper().endswith(".BJ")


def _normalize_universe(raw: list[dict], panel: str) -> pd.DataFrame:
    rows = []
    for item in raw:
        code = str(item.get("ts_code") or "").upper()
        name = str(item.get("name") or "")
        if not code or "." not in code:
            continue
        if _is_bj(code) or code.split(".")[0].startswith("92"):
            continue
        rows.append({"ts_code": code, "name": name, "board": panel})
    return (
        pd.DataFrame(rows)
        .drop_duplicates("ts_code")
        .reset_index(drop=True)  # preserve market-cap order
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
    try:
        import akshare as ak
    except ImportError as exc:
        return code, None, f"akshare missing: {exc}"

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
    from concurrent.futures import ThreadPoolExecutor, as_completed

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


def _prepare(
    dst: Path,
    codes: list[str],
    data_map: dict,
    engine_src: Path,
    strategy: str,
    sec: Sector,
) -> None:
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
            "panel": sec.panel,
            "engine": "F1_veto_max20" if "F1" in strategy else "F0_dual",
            "universe": sec.universe_desc,
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


def _write_prop(
    sec: Sector,
    paths: dict[str, Path],
    winner: str,
    f0: dict,
    f1: dict,
) -> None:
    m0, m1 = f0["metrics"], f1["metrics"]
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    if winner == "F1":
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上的最优复现：F1 = F0 硬确认 + custom_max_inv 底部否决。
同池实测（run `{paths['f1'].name}` vs `{paths['f0'].name}`）：
- F1 相对 F0：Δtotal={_pct(d_total)}，Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}
- 结构依据仍来自 BK1184 bake-off（veto_max_20）；本版不重做因子挖掘。

本版只做一件事：在{sec.name}上复用 F0 + custom_max_inv 底部否决，不改过程约束。
优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认；若不及同池 F0 明文写「F1 未在{sec.name}带来增量」；若明显弱于板块基准明文写「F1 结构未在{sec.name}迁移成功」，禁止为追收益擅自加反转/扩因子。

【股票池与区间】
- 标的池：A股{sec.name}相关板块（{sec.universe_desc}）
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
- 3–5 条白话：为何否决不同于 E 硬过滤、相对基准、过拟合风险（含{sec.risk_note}）
- 声明：研究/回测，非实盘建议

```
"""
    else:
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上的最优复现：F0 = 动量 164 + 硬确认 171/054。
同池实测 F1（MAX 底部否决）未带来增量（Δexcess={_pct(d_excess)}，Δsharpe={_num(d_sharpe)}），故默认回退 F0。
禁止为追收益叠加 custom 否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股{sec.name}相关板块（{sec.universe_desc}）
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
- 3–5 条白话：相对基准、{sec.risk_note}/过拟合风险
- 声明：研究/回测，非实盘建议

```
"""
    paths["prop"].write_text(body, encoding="utf-8")
    print(f"wrote prop {paths['prop']} (winner={winner})")


def _write_reports(
    sec: Sector,
    paths: dict[str, Path],
    f0: dict,
    f1: dict,
    name_map: dict[str, str],
    listed_n: int,
    used_n: int,
) -> str:
    m0, m1 = f0["metrics"], f1["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(paths["f0"], name_map)
    hold1_md, h1 = _holdings(paths["f1"], name_map)
    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])
    winner = "F1" if d_excess > 0 else "F0"
    panel = sec.panel

    eq = pd.read_csv(paths["f0"] / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    compare = f"""# F0 vs F1 对比报告 — A股{sec.name}（{panel}）

**声明：研究/回测结果，非实盘建议。**

## 1. 研究设定

| 项 | 内容 |
|----|------|
| 板块 | **{panel}** |
| 成分 | {sec.universe_desc}；原始 {listed_n} → 市值优先截断后 {used_n} → 有效 OHLCV {f0['n_symbols']} |
| 区间 | 配置 {START_DATE} → {END_DATE}；实际 {actual_start} → {actual_end} |
| F0 | 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054` |
| F1 | F0 + `custom_max_inv` 底部 20% 否决新开仓 |
| F0 run | `{paths['f0'].name}` |
| F1 run | `{paths['f1'].name}` |
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
            f"F1 {_pct(m1['excess_return'])}），相对{sec.name}等权基准均未跑赢。\n"
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
2. {sec.name}受{sec.risk_note}驱动，单板块结果易随市场周期过拟合。
3. 主判据是相对同池的 Δexcess；若 F1 无增量应回退 F0，禁止为追收益擅自加反转/扩因子。
4. 成分取自 {sec.universe_desc}。
5. 研究/回测，非实盘建议。

---

Artifacts：`{paths['f0'].resolve()}` / `{paths['f1'].resolve()}`
"""
    paths["compare"].write_text(compare, encoding="utf-8")
    (paths["f0"] / f"REPORT_COMPARE_{sec.slug}.md").write_text(compare, encoding="utf-8")
    (paths["f1"] / f"REPORT_COMPARE_{sec.slug}.md").write_text(compare, encoding="utf-8")

    for run, title, prompt in (
        (
            paths["f0"],
            f"F0双确认-{sec.name}{sec.bk_label}",
            f"请用国泰君安191做F0双确认多因子选股并回测：A股{sec.name}（{sec.bk_label}）",
        ),
        (
            paths["f1"],
            f"F1 MAX否决-{sec.name}{sec.bk_label}",
            f"请用国泰君安191+custom_max_inv做F1多因子选股并回测：A股{sec.name}（{sec.bk_label}）",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {"panel": panel, "universe_run": paths["src"].name},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    _write_prop(sec, paths, winner, f0, f1)
    print(f"wrote reports for {sec.name}; winner={winner}")
    return winner


def run_one(sec: Sector) -> dict[str, Any]:
    paths = _paths(sec)
    print("\n" + "#" * 72)
    print(f"# {sec.name} ({sec.bk_label})")
    print("#" * 72)

    payload = fetch_universe(sec, paths["univ_json"])
    univ = _normalize_universe(payload["codes"], sec.panel)
    listed_n = len(payload["codes"])
    if len(univ) > MAX_UNIVERSE:
        univ = univ.head(MAX_UNIVERSE).copy()
    used_n = len(univ)
    print(f"[universe] raw={listed_n} after_ex_BJ_cap={used_n}")
    if used_n < 20:
        raise SystemExit(f"{sec.name}: universe too small: {used_n}")

    paths["src"].mkdir(parents=True, exist_ok=True)
    univ.to_csv(paths["src"] / "universe.csv", index=False, encoding="utf-8-sig")
    (paths["src"] / "universe_meta.json").write_text(
        json.dumps(
            {
                "panel": sec.panel,
                "source": sec.universe_desc,
                "listed_raw": listed_n,
                "ex_bj_capped": used_n,
                "max_universe": MAX_UNIVERSE,
                "by_bk": payload.get("by_bk"),
                "codes": univ["ts_code"].tolist(),
                "names": {r.ts_code: r.name for r in univ.itertuples()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    data_map = fetch_ohlcv(univ["ts_code"].tolist(), paths["src"] / "artifacts")
    if len(data_map) < 20:
        raise SystemExit(f"{sec.name}: too few OHLCV: {len(data_map)}")
    name_map = {r.ts_code: r.name for r in univ.itertuples() if r.ts_code in data_map}
    codes = [c for c in univ["ts_code"].tolist() if c in data_map]
    (paths["src"] / "config.json").write_text(
        json.dumps(
            {
                "codes": codes,
                "source": "tushare",
                "start_date": START_DATE,
                "end_date": END_DATE,
                "interval": "1D",
                "initial_cash": 1_000_000,
                "parameters": {"panel": sec.panel},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 60, "F0")
    _prepare(paths["f0"], codes, data_map, ENGINE_F0, f"F0_{sec.slug}", sec)
    f0 = _run(paths["f0"], data_map)
    print("F0 excess", f0["metrics"].get("excess_return"))

    print("=" * 60, "F1")
    _prepare(paths["f1"], codes, data_map, ENGINE_F1, f"F1_{sec.slug}", sec)
    f1 = _run(paths["f1"], data_map)
    print("F1 excess", f1["metrics"].get("excess_return"))

    winner = _write_reports(sec, paths, f0, f1, name_map, listed_n, used_n)
    m0, m1 = f0["metrics"], f1["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    return {
        "name": sec.name,
        "bk": sec.bk_label,
        "slug": sec.slug,
        "winner": winner,
        "n_symbols": f0["n_symbols"],
        "listed_n": listed_n,
        "used_n": used_n,
        "f0_excess": float(m0["excess_return"]),
        "f1_excess": float(m1["excess_return"]),
        "f0_total": float(m0["total_return"]),
        "f1_total": float(m1["total_return"]),
        "f0_sharpe": float(m0["sharpe"]),
        "f1_sharpe": float(m1["sharpe"]),
        "f0_mcp": v0.get("p_value_sharpe"),
        "f1_mcp": v1.get("p_value_sharpe"),
        "d_excess": float(m1["excess_return"]) - float(m0["excess_return"]),
        "d_sharpe": float(m1["sharpe"]) - float(m0["sharpe"]),
        "compare": str(paths["compare"]),
        "prop": str(paths["prop"]),
        "f0_run": paths["f0"].name,
        "f1_run": paths["f1"].name,
    }


def _write_summary(rows: list[dict]) -> None:
    lines = [
        "# F0 vs F1 汇总 — 高可持续科技三板块（2026-07-31 题材）",
        "",
        "**声明：研究/回测结果，非实盘建议。**",
        "",
        "## 研究设定",
        "",
        "| 项 | 内容 |",
        "|----|------|",
        "| 选板口径 | 行情日报「高可持续」：存储芯片 / 半导体设备材料 / 算力硬件 |",
        f"| 区间 | {START_DATE} → {END_DATE} |",
        f"| 池截断 | 市值优先最多 {MAX_UNIVERSE} 只（排除北交所） |",
        "| F0 | 164 动量 + 171/054 硬确认 |",
        "| F1 | F0 + custom_max_inv 底部 20% 否决 |",
        "| 主判据 | excess_return；辅看 MC p_sharpe / sharpe |",
        "",
        "## 核心对照",
        "",
        "| 板块 | 有效池 | F0 excess | F1 excess | Δexcess | F0 Sharpe | F1 Sharpe | F0 MC p | F1 MC p | 最优 |",
        "|------|--------|-----------|-----------|---------|-----------|-----------|---------|---------|------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['name']} ({r['bk']}) | {r['n_symbols']} | "
            f"{_pct(r['f0_excess'])} | {_pct(r['f1_excess'])} | {_pct(r['d_excess'])} | "
            f"{_num(r['f0_sharpe'])} | {_num(r['f1_sharpe'])} | "
            f"{_num(r['f0_mcp'])} | {_num(r['f1_mcp'])} | **{r['winner']}** |"
        )

    lines += ["", "## 分板块报告", ""]
    for r in rows:
        lines.append(
            f"- **{r['name']}**：最优 `{r['winner']}` · "
            f"[对比]({Path(r['compare']).name}) · [最优提示词]({Path(r['prop']).name}) · "
            f"`{r['f0_run']}` / `{r['f1_run']}`"
        )

    lines += ["", "## 门控解读", ""]
    for r in rows:
        notes = []
        best_ex = r["f1_excess"] if r["winner"] == "F1" else r["f0_excess"]
        best_p = r["f1_mcp"] if r["winner"] == "F1" else r["f0_mcp"]
        if best_ex < 0:
            notes.append("最优策略仍负超额")
        if best_p is not None and float(best_p) > 0.2:
            notes.append("MC 不显著")
        if not notes:
            notes.append("excess>0 且 MC 过线（样本内）")
        lines.append(f"- {r['name']}：{'；'.join(notes)}")

    lines += ["", "---", "", f"Run prefix：`{RUN_PREFIX}`"]
    SUMMARY_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote summary {SUMMARY_OUT}")


def main() -> int:
    if not ENGINE_F0.exists() or not ENGINE_F1.exists():
        raise SystemExit(f"missing engines: {ENGINE_F0} / {ENGINE_F1}")

    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = SECTORS
    if only:
        selected = [
            s
            for s in SECTORS
            if s.slug in only or s.name in only or any(b in only for b in s.bks)
        ]
        if not selected:
            raise SystemExit(f"no sector matched args={only}")

    results: list[dict] = []
    errors: list[str] = []
    t0 = time.time()
    for sec in selected:
        try:
            results.append(run_one(sec))
        except Exception as exc:  # noqa: BLE001
            msg = f"{sec.name} ({sec.bk_label}) FAILED: {exc}"
            print(msg)
            traceback.print_exc()
            errors.append(msg)

    if results:
        _write_summary(results)
        print("\n=== DONE ===")
        for r in results:
            print(
                f"  {r['name']}: winner={r['winner']} "
                f"Δexcess={_pct(r['d_excess'])} n={r['n_symbols']}"
            )
    print(f"elapsed {time.time()-t0:.1f}s")
    if errors:
        print("errors:")
        for e in errors:
            print(" ", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
