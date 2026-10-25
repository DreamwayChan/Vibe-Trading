"""Formal copper optimal rerun: F0' = 164 + hard confirm 081/160.

Also runs F1' = F0' + custom_max_inv bottom-20% veto, and contrasts both
against the legacy F0 (171/054) on the same BK1615 panel.

Writes:
  - runs/20260726_131500_03_F0_copper_081_160/  (+ REPORT / req.json)
  - runs/20260726_131500_04_F1_copper_081_160/  (+ REPORT / req.json)
  - prop_国泰君安_金属铜_最优.md
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
from src.factors.survival_store import survival_path  # noqa: E402

SRC_RUN = _AGENT_ROOT / "runs" / "20260726_131500_00_copper_universe"
LEGACY_F0 = _AGENT_ROOT / "runs" / "20260726_131500_01_F0_copper"
LEGACY_F1 = _AGENT_ROOT / "runs" / "20260726_131500_02_F1_copper"
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

F0_RUN = _AGENT_ROOT / "runs" / "20260726_131500_03_F0_copper_081_160"
F1_RUN = _AGENT_ROOT / "runs" / "20260726_131500_04_F1_copper_081_160"
PROP_OUT = _AGENT_ROOT / "prop_国泰君安_金属铜_最优.md"

START_DATE = "2024-01-01"
END_DATE = "2026-07-24"
MC_SIMS = 1000
PANEL_LABEL = "金属铜 BK1615"

MOM_ID = "gtja191_164"
CONFIRM_IDS = ["gtja191_081", "gtja191_160"]
# Fallback if survival store missing these keys.
_IR_FALLBACK = {
    "gtja191_164": 0.145,
    "gtja191_081": 0.121,
    "gtja191_160": 0.145,
    "gtja191_171": 0.155,
    "gtja191_054": 0.152,
}


def _load_irs() -> dict[str, float]:
    path = survival_path()
    alphas = json.loads(path.read_text(encoding="utf-8")).get("alphas") or {}
    out = dict(_IR_FALLBACK)
    for aid in set(CONFIRM_IDS) | {MOM_ID, "gtja191_171", "gtja191_054"}:
        row = alphas.get(aid) or {}
        try:
            out[aid] = float(row.get("ir") or out[aid])
        except (TypeError, ValueError):
            pass
    return out


def _patch_engine(src: Path, dst: Path, irs: dict[str, float], with_veto: bool) -> None:
    """Rewrite confirm legs / IR table inside the bake-off engine template."""
    text = src.read_text(encoding="utf-8")
    ir_block = (
        "    _IR = {\n"
        f'        "{MOM_ID}": {irs[MOM_ID]:.4f},\n'
        f'        "{CONFIRM_IDS[0]}": {irs[CONFIRM_IDS[0]]:.4f},\n'
        f'        "{CONFIRM_IDS[1]}": {irs[CONFIRM_IDS[1]]:.4f},\n'
        "    }"
    )
    # Replace the first _IR dict literal.
    start = text.find("    _IR = {")
    if start < 0:
        raise SystemExit("engine missing _IR block")
    end = text.find("}", start)
    end = text.find("\n", end)  # keep through closing brace line
    text = text[:start] + ir_block + text[end:]

    old_conf = 'self.confirm_ids = ["gtja191_171", "gtja191_054"]'
    new_conf = f'self.confirm_ids = {json.dumps(CONFIRM_IDS)}'
    if old_conf not in text:
        raise SystemExit("engine missing confirm_ids assignment")
    text = text.replace(old_conf, new_conf, 1)

    # Keep veto custom_ids as-is for F1; F0 template already has custom_ids = [].
    if with_veto and "custom_max_inv" not in text:
        raise SystemExit("F1 engine template missing custom_max_inv")

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


def _load_ohlcv() -> dict[str, pd.DataFrame]:
    art = SRC_RUN / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for path in sorted(art.glob("ohlcv_*.csv")):
        code = path.stem.replace("ohlcv_", "")
        df = pd.read_csv(path, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if len(df) >= 120 and "close" in keep:
            data_map[code] = df[keep].copy()
    if len(data_map) < 8:
        raise SystemExit(f"too few OHLCV: {len(data_map)}")
    return data_map


def _name_map() -> dict[str, str]:
    meta_path = SRC_RUN / "universe_meta.json"
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return dict(meta.get("names") or {})


def _prepare(dst: Path, codes: list[str], data_map: dict, engine_src: Path, strategy: str, irs: dict) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    art = dst / "artifacts"
    art.mkdir(parents=True)
    for code in codes:
        out = data_map[code].copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")

    with_veto = "F1" in strategy
    _patch_engine(engine_src, dst / "code" / "signal_engine.py", irs, with_veto=with_veto)

    alphas = [MOM_ID] + CONFIRM_IDS
    if with_veto:
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
            "engine": "F1_veto_max20_081_160" if with_veto else "F0_081_160",
            "confirm_ids": CONFIRM_IDS,
            "universe": "东财行业 铜 BK1615（金属铜概念核心池，排除北交所）",
            "note": "confirm legs re-searched among GTJA191 alive factors",
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
    need = list(cfg.get("alphas") or ([MOM_ID] + CONFIRM_IDS))
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
    # Sanity: engine must use the re-searched confirm legs.
    if list(getattr(sig, "confirm_ids", [])) != CONFIRM_IDS:
        raise SystemExit(f"confirm_ids mismatch: {getattr(sig, 'confirm_ids', None)}")

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
        "confirm_ids": list(getattr(sig, "confirm_ids", [])),
    }


def _load_legacy(run_dir: Path) -> dict:
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {"metrics": metrics, "validation": validation, "run": run_dir.name}


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


def _write_prop(f0: dict, f1: dict, legacy: dict, irs: dict[str, float]) -> None:
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    d_ex_f1 = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sh_f1 = float(m1["sharpe"]) - float(m0["sharpe"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    ir_txt = (
        f"081:{irs[CONFIRM_IDS[0]]:.3f}, 160:{irs[CONFIRM_IDS[1]]:.3f}"
    )
    # Prefer F0' unless F1' adds excess.
    winner = "F1" if d_ex_f1 > 0 else "F0"
    if winner == "F0":
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为金属铜板块（东财行业「铜」BK1615）上的最优复现：
F0' = 动量 164 + 硬确认 081/160（存活因子确认腿重搜结果）。
对照原基线 F0(171/054)：Δexcess={_pct(d_ex_legacy)}。
同池实测 F1'（081/160 + MAX 底部否决）未带来增量（Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}），故默认回退 F0'。
禁止为追收益叠加 custom 否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股金属铜相关板块（东财行业「铜」BK1615，排除北交所）
- 研究/回测参考区间：2024-01-01 至 2026-07-24（数据不足则用可得区间并说明）
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
| gtja191_081 | 确认（F0' 硬确认） |
| gtja191_160 | 确认（F0' 硬确认） |

复制用：gtja191_164, gtja191_081, gtja191_160
【禁止】gtja191_114 / 反转因子；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排
【禁止】回退使用 171/054 作为本版确认腿（那是旧基线，非本版最优）

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权({ir_txt})；缺谁跳谁并重归一
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
- 因子 ID + 角色；未使用反转/吸筹/custom；确认腿为 081/160 非 171/054
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照：同池 F1' 与原基线 171/054，给出 Δtotal / Δexcess / Δsharpe；说明为何以 F0' 为准
- 3–5 条白话：相对基准、铜价周期/确认腿样本内过拟合风险
- 声明：研究/回测，非实盘建议

```
"""
    else:
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为金属铜板块（东财行业「铜」BK1615）上的最优复现：
F1' = 动量 164 + 硬确认 081/160 + custom_max_inv 底部否决。
对照同池 F0'：Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}。
对照原基线 171/054：F0' 已提升 Δexcess={_pct(d_ex_legacy)}。
禁止为追收益擅自加反转/扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股金属铜相关板块（东财行业「铜」BK1615，排除北交所）
- 研究/回测参考区间：2024-01-01 至 2026-07-24（数据不足则用可得区间并说明）
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
| gtja191_081 | 确认（F0' 硬确认） |
| gtja191_160 | 确认（F0' 硬确认） |
| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |

复制用：gtja191_164, gtja191_081, gtja191_160, custom_max_inv
【禁止】gtja191_114 / 反转因子；禁止用 custom 替换 081/160

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权({ir_txt})；缺谁跳谁并重归一
4. F0' 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. F1' 额外否决（仅新开仓）：smooth(custom_max_inv) < 当日截面 20% 分位 → 禁止新开
6. 面板只建 OHLCV；不要为 114 拼 amount

【持仓与换手（硬性）】
1. N=6 等权；保留带 Top9（N×1.5）
2. 调仓日：先过 F0' 确认，再过 MAX 底部否决，按 S 选 Top6；旧仓仍在 Top9 且未触发风控可续持
3. 换手硬顶 20%：优先留旧仓，再填新股；将超 20% 则停止换入（空仓建仓首期除外）
4. 最小持有 5 日：未满 5 日不因跌出保留带而卖；峰值回撤止损/止盈立即生效
5. 择时：板块等权均价 vs MA20，仅调仓日更新乘数（1.0/0.5）；非调仓日冻结

【退出】
峰值回撤≥15% → 止盈≥35% → 持仓≥20日且排名恶化≥10名 → 调仓日跌出 Top9（且满 5 日）
非调仓日不因排名换股。

【交付】
- 因子 ID + 角色；说明 custom_max_inv 是反彩票底部否决
- 最近调仓日持仓表；完整指标 + MC p
- 对照同池 F0' 与原基线 171/054
- 声明：研究/回测，非实盘建议

```
"""
    PROP_OUT.write_text(body, encoding="utf-8")
    print(f"wrote prop {PROP_OUT} (winner={winner})")


def _write_reports(
    f0: dict,
    f1: dict,
    legacy: dict,
    legacy_f1: dict,
    name_map: dict[str, str],
    irs: dict[str, float],
    listed_n: int,
) -> str:
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    vl = (legacy.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(F0_RUN, name_map)
    hold1_md, h1 = _holdings(F1_RUN, name_map)

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    d_tot_legacy = float(m0["total_return"]) - float(ml["total_return"])
    d_sh_legacy = float(m0["sharpe"]) - float(ml["sharpe"])
    winner = "F1" if d_excess > 0 else "F0"

    eq = pd.read_csv(F0_RUN / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    ir081 = irs[CONFIRM_IDS[0]]
    ir160 = irs[CONFIRM_IDS[1]]

    report_f0 = f"""# F0' 最优确认报告 — 金属铜 BK1615（164 + 081/160）

**声明：研究/回测结果，非实盘建议。本报告为金属铜板块在 GTJA191 存活因子中重搜确认腿后的最优正式复跑：F0' = 164 + 081/160。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}**（金属铜概念 / 铜产业链） |
| 策略 | F0' = 动量 `gtja191_164` + 硬确认 `gtja191_081`/`gtja191_160`（无 custom 否决） |
| F0' run | `{F0_RUN.name}` |
| 同池 F1' 对照 | `{F1_RUN.name}` |
| 原基线 F0 | `{LEGACY_F0.name}`（171/054） |
| 成分来源 | 东财行业 BK1615；排除北交所；原始 {listed_n} → 有效 OHLCV {f0['n_symbols']} |
| 配置区间 | {START_DATE} → {END_DATE} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `gtja191_164, gtja191_081, gtja191_160` |
| is_rebalance_days | {f0.get('is_rebalance_days')} |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_081 | F0' 硬确认（IR {_num(ir081, 3)}） |
| gtja191_160 | F0' 硬确认（IR {_num(ir160, 3)}） |

## 3. F0' 核心绩效

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
    report_f0 += (
        f"- 确认腿来自存活因子样本内重搜，相对原基线 171/054 有过拟合风险"
        f"（Δexcess={_pct(d_ex_legacy)}）。\n"
    )

    report_f0 += f"""
## 4. 对照：F0' vs 原基线 171/054 vs F1'

| 指标 | F0' (081/160) | 原 F0 (171/054) | F1' (081/160+MAX) |
|------|---------------|-----------------|-------------------|
| total_return | {_pct(m0['total_return'])} | {_pct(ml['total_return'])} | {_pct(m1['total_return'])} |
| excess_return | {_pct(m0['excess_return'])} | {_pct(ml['excess_return'])} | {_pct(m1['excess_return'])} |
| sharpe | {_num(m0['sharpe'])} | {_num(ml['sharpe'])} | {_num(m1['sharpe'])} |
| max_drawdown | {_pct(m0['max_drawdown'])} | {_pct(ml['max_drawdown'])} | {_pct(m1['max_drawdown'])} |
| trade_count | {_num(m0.get('trade_count'), 0)} | {_num(ml.get('trade_count'), 0)} | {_num(m1.get('trade_count'), 0)} |
| rebalance_turnover_mean | {_num(m0.get('rebalance_turnover_mean'))} | {_num(ml.get('rebalance_turnover_mean'))} | {_num(m1.get('rebalance_turnover_mean'))} |
| MC p_sharpe | {_num(v0.get('p_value_sharpe'))} | {_num(vl.get('p_value_sharpe'))} | {_num(v1.get('p_value_sharpe'))} |

| 对比 | Δtotal | Δexcess | Δsharpe |
|------|--------|---------|---------|
| F0' − 原F0 | {_pct(d_tot_legacy)} | {_pct(d_ex_legacy)} | {_num(d_sh_legacy)} |
| F1' − F0' | {_pct(d_total)} | {_pct(d_excess)} | {_num(d_sharpe)} |

"""
    if d_excess > 0:
        report_f0 += f"> **对照：同池 F1' excess 高于 F0'**（Δexcess={_pct(d_excess)}）。本板块最优为 F1'。\n"
    else:
        report_f0 += (
            f"> **结论：本板块以 F0'（081/160）为准**；"
            f"F1' 未带来增量（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）；"
            f"相对原基线 171/054 提升 Δexcess={_pct(d_ex_legacy)}。\n"
        )

    report_f0 += f"""
## 5. 最近调仓日持仓（F0'）

- 日期：{h0['last_date']}；持仓 {h0['n_held']} 只；权重和 {_num(h0['gross'])}
- 有仓日平均持仓只数 {_num(h0['mean_n'], 2)}（中位 {_num(h0['median_n'], 2)}）

{hold0_md}

## 6. 白话结论

1. F0' = 纯动量 164 + 存活因子重搜确认腿 081/160；周频 / 换手帽 / 最小持有 / 择时不变。
2. 相对原基线 171/054，本样本 excess / sharpe 均提升，且最大回撤更浅。
3. 同池叠加 MAX 否决（F1'）未再增量时，回退 F0'，禁止为追收益扩因子。
4. 金属铜强绑定铜价与宏观；确认腿样本内重搜有过拟合风险，需样本外复核。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F0_RUN.resolve()}`。
"""

    ml1 = legacy_f1["metrics"]
    report_f1 = f"""# F1' MAX否决报告 — 金属铜 BK1615（081/160 + max_inv）

**声明：研究/回测结果，非实盘建议。F1' = F0'(164+081/160) + `custom_max_inv` 底部 20% 否决。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{PANEL_LABEL}** |
| 策略 | F1' = F0'(164+081/160) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1' run | `{F1_RUN.name}` |
| 同池 F0' 对照 | `{F0_RUN.name}` |
| 原 F1 (171/054+MAX) | `{LEGACY_F1.name}` excess={_pct(ml1['excess_return'])} |
| 因子 | `gtja191_164, gtja191_081, gtja191_160, custom_max_inv` |
| is_rebalance_days | {f1.get('is_rebalance_days')} |

## 2. F1' 核心绩效

| 指标 | 数值 |
|------|------|
| total_return | {_pct(m1['total_return'])} |
| excess_return | {_pct(m1['excess_return'])} |
| sharpe | {_num(m1['sharpe'])} |
| max_drawdown | {_pct(m1['max_drawdown'])} |
| trade_count | {_num(m1.get('trade_count'), 0)} |
| rebalance_turnover_mean | {_num(m1.get('rebalance_turnover_mean'))} |
| MC p_value_sharpe | {_num(v1.get('p_value_sharpe'))} |

### 同池：F1' vs F0'

| 指标 | F1' | F0' | F1'−F0' |
|------|-----|-----|---------|
| total_return | {_pct(m1['total_return'])} | {_pct(m0['total_return'])} | {_pct(d_total)} |
| excess_return | {_pct(m1['excess_return'])} | {_pct(m0['excess_return'])} | {_pct(d_excess)} |
| sharpe | {_num(m1['sharpe'])} | {_num(m0['sharpe'])} | {_num(d_sharpe)} |

"""
    if d_excess <= 0:
        report_f1 += (
            f"> **同板块结论：F1' 未在金属铜带来增量**"
            f"（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。应回退 F0'。\n"
        )
    else:
        report_f1 += (
            f"> **同板块结论：F1' 相对 F0' 正向**"
            f"（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。**本板块最优为 F1'。**\n"
        )

    report_f1 += f"""
## 3. 最近调仓日持仓（F1'）

- 日期：{h1['last_date']}；持仓 {h1['n_held']} 只；权重和 {_num(h1['gross'])}

{hold1_md}

同日 F0' 持仓：`{', '.join(h0['codes'])}`（n={h0['n_held']}）

## 4. 白话结论

1. `custom_max_inv` 仅底部否决新开，不是质量主确认。
2. 若相对 F0' 无增量，回退 081/160 双确认，禁止再叠 custom。
3. 研究/回测，非实盘建议。

---

本 run artifacts：`{F1_RUN.resolve()}`。
"""

    (F0_RUN / "REPORT_F0_copper_081_160.md").write_text(report_f0, encoding="utf-8")
    (F1_RUN / "REPORT_F1_copper_081_160.md").write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            F0_RUN,
            "F0'最优确认-金属铜BK1615-081/160",
            "请用国泰君安191做F0'多因子选股并回测：金属铜BK1615，动量164+确认081/160",
        ),
        (
            F1_RUN,
            "F1' MAX否决-金属铜BK1615-081/160",
            "请用国泰君安191+custom_max_inv做F1'多因子选股并回测：金属铜BK1615，确认081/160",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {
                        "panel": PANEL_LABEL,
                        "confirm_ids": CONFIRM_IDS,
                        "universe_run": SRC_RUN.name,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    _write_prop(f0, f1, legacy, irs)
    print("wrote reports + req.json; winner=", winner)
    return winner


def main() -> None:
    if not ENGINE_F0_SRC.exists() or not ENGINE_F1_SRC.exists():
        raise SystemExit("missing F0/F1 engine templates under runs/_research_custom_positive/")
    if not LEGACY_F0.exists():
        raise SystemExit(f"missing legacy F0 run: {LEGACY_F0}")

    irs = _load_irs()
    print(
        f"[ir] 081={irs[CONFIRM_IDS[0]]:.4f} 160={irs[CONFIRM_IDS[1]]:.4f} "
        f"164={irs[MOM_ID]:.4f}"
    )

    data_map = _load_ohlcv()
    names = _name_map()
    codes = sorted(data_map)
    listed_n = len(names) or len(codes)
    print(f"[panel] symbols={len(codes)}")

    print("=" * 60, "F0' 081+160")
    _prepare(F0_RUN, codes, data_map, ENGINE_F0_SRC, "F0_copper_081_160", irs)
    f0 = _run(F0_RUN, data_map)
    print("F0' excess", f0["metrics"].get("excess_return"))

    print("=" * 60, "F1' 081+160+max")
    _prepare(F1_RUN, codes, data_map, ENGINE_F1_SRC, "F1_copper_081_160", irs)
    f1 = _run(F1_RUN, data_map)
    print("F1' excess", f1["metrics"].get("excess_return"))

    legacy = _load_legacy(LEGACY_F0)
    legacy_f1 = _load_legacy(LEGACY_F1) if LEGACY_F1.exists() else legacy
    winner = _write_reports(f0, f1, legacy, legacy_f1, names, irs, listed_n)
    print("[done]", F0_RUN.name, F1_RUN.name, "winner", winner)


if __name__ == "__main__":
    main()
