"""Formal F0'/F1' reruns with confirm legs 081/160 on robot + memory panels.

For each sector:
  - F0'  = 164 + hard confirm 081/160
  - F1'  = F0' + custom_max_inv bottom-20% veto
  - legacy F0 = 164 + 171/054 (re-run on same OHLCV for fair Δ)

Writes per-sector REPORT / req.json and updates prop_国泰君安_{name}_最优.md.

Run from agent/:
    python scripts/run_sectors_optimal_081_160.py
    python scripts/run_sectors_optimal_081_160.py --only robot
    python scripts/run_sectors_optimal_081_160.py --only memory
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
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

MOM_ID = "gtja191_164"
CONFIRM_NEW = ["gtja191_081", "gtja191_160"]
CONFIRM_LEGACY = ["gtja191_171", "gtja191_054"]
MC_SIMS = 1000

_IR_FALLBACK = {
    "gtja191_164": 0.145,
    "gtja191_081": 0.121,
    "gtja191_160": 0.145,
    "gtja191_171": 0.155,
    "gtja191_054": 0.152,
}


@dataclass(frozen=True)
class SectorJob:
    key: str
    name: str
    panel: str
    universe_desc: str
    ohlcv_src: Path
    start_date: str
    end_date: str
    initial_cash: float
    risk_note: str
    f0_run: Path
    f1_run: Path
    legacy_f0_run: Path
    prop_out: Path
    # Optional published legacy F1 path (171/054+MAX) for report contrast only.
    published_f1: Path | None = None


SECTORS: list[SectorJob] = [
    SectorJob(
        key="robot",
        name="人形机器人",
        panel="人形机器人 BK1184",
        universe_desc="东财概念 人形机器人 BK1184（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=10_000_000,
        risk_note="主题拥挤、订单兑现与估值波动；确认腿样本内迁移有过拟合风险",
        f0_run=_AGENT_ROOT / "runs" / "20260725_184000_03_F0_robot_081_160",
        f1_run=_AGENT_ROOT / "runs" / "20260725_184000_04_F1_robot_081_160",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260725_184000_03_F0_robot_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_人形机器人_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
    ),
    SectorJob(
        key="memory",
        name="存储芯片",
        panel="存储芯片 BK1137",
        universe_desc="东财 存储芯片 BK1137（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe",
        start_date="2024-01-01",
        end_date="2026-07-31",
        initial_cash=1_000_000,
        risk_note="存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动；确认腿样本内迁移有过拟合风险",
        f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_03_F0_memory_081_160",
        f1_run=_AGENT_ROOT / "runs" / "20260731_tech3_04_F1_memory_081_160",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_03_F0_memory_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_存储芯片_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260731_tech3_02_F1_memory",
    ),
]


def _load_irs() -> dict[str, float]:
    path = survival_path()
    alphas = json.loads(path.read_text(encoding="utf-8")).get("alphas") or {}
    out = dict(_IR_FALLBACK)
    for aid in set(CONFIRM_NEW + CONFIRM_LEGACY + [MOM_ID]):
        row = alphas.get(aid) or {}
        try:
            out[aid] = float(row.get("ir") or out[aid])
        except (TypeError, ValueError):
            pass
    return out


def _patch_engine(
    src: Path,
    dst: Path,
    confirm_ids: list[str],
    irs: dict[str, float],
) -> None:
    """Rewrite confirm legs / IR table inside the bake-off engine template."""
    text = src.read_text(encoding="utf-8")
    ir_lines = [f'        "{MOM_ID}": {irs[MOM_ID]:.4f},']
    for aid in confirm_ids:
        ir_lines.append(f'        "{aid}": {irs[aid]:.4f},')
    ir_block = "    _IR = {\n" + "\n".join(ir_lines) + "\n    }"

    start = text.find("    _IR = {")
    if start < 0:
        raise SystemExit("engine missing _IR block")
    end = text.find("}", start)
    end = text.find("\n", end)
    text = text[:start] + ir_block + text[end:]

    old_conf = 'self.confirm_ids = ["gtja191_171", "gtja191_054"]'
    new_conf = f"self.confirm_ids = {json.dumps(confirm_ids)}"
    if old_conf not in text:
        # Already patched template from a prior copper run — replace whatever is there.
        import re

        text2, n = re.subn(
            r"self\.confirm_ids = \[[^\]]+\]",
            new_conf,
            text,
            count=1,
        )
        if n != 1:
            raise SystemExit("engine missing confirm_ids assignment")
        text = text2
    else:
        text = text.replace(old_conf, new_conf, 1)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


def _load_ohlcv(src: Path) -> dict[str, pd.DataFrame]:
    art = src / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for path in sorted(art.glob("ohlcv_*.csv")):
        code = path.stem.replace("ohlcv_", "", 1)
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if len(df) >= 120 and "close" in keep:
            data_map[code] = df[keep].copy()
    if len(data_map) < 8:
        raise SystemExit(f"{src.name}: too few OHLCV ({len(data_map)})")
    return data_map


def _name_map(sec: SectorJob) -> dict[str, str]:
    # Prefer universe_meta next to OHLCV source or sibling universe run.
    candidates = [
        sec.ohlcv_src / "universe_meta.json",
        sec.ohlcv_src.parent / f"{sec.ohlcv_src.name.replace('_F1_', '_00_').replace('01_F1', '00')}",
    ]
    # Memory: explicit universe run.
    if sec.key == "memory":
        candidates.insert(0, _AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe" / "universe_meta.json")
    for path in candidates:
        if path.is_file() and path.suffix == ".json":
            meta = json.loads(path.read_text(encoding="utf-8"))
            names = meta.get("names") or {}
            if names:
                return {str(k): str(v) for k, v in names.items()}
        # Also try universe.csv
    univ_csv = None
    if sec.key == "memory":
        univ_csv = _AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe" / "universe.csv"
    elif (sec.ohlcv_src / "universe.csv").exists():
        univ_csv = sec.ohlcv_src / "universe.csv"
    if univ_csv and univ_csv.exists():
        df = pd.read_csv(univ_csv)
        code_col = "ts_code" if "ts_code" in df.columns else "code"
        name_col = "name" if "name" in df.columns else None
        if name_col:
            return {
                str(r[code_col]): str(r[name_col])
                for _, r in df.iterrows()
                if pd.notna(r.get(code_col))
            }
    return {}


def _prepare(
    dst: Path,
    sec: SectorJob,
    data_map: dict[str, pd.DataFrame],
    engine_src: Path,
    confirm_ids: list[str],
    irs: dict[str, float],
    strategy: str,
) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    art = dst / "artifacts"
    art.mkdir(parents=True)
    codes = sorted(data_map)
    for code in codes:
        out = data_map[code].copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")

    _patch_engine(engine_src, dst / "code" / "signal_engine.py", confirm_ids, irs)

    alphas = [MOM_ID] + list(confirm_ids)
    if "F1" in strategy:
        alphas = alphas + ["custom_max_inv"]
    cfg = {
        "codes": codes,
        "source": "tushare",
        "start_date": sec.start_date,
        "end_date": sec.end_date,
        "interval": "1D",
        "initial_cash": sec.initial_cash,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": alphas,
        "parameters": {
            "strategy": strategy,
            "panel": sec.panel,
            "confirm_ids": confirm_ids,
            "universe": sec.universe_desc,
            "ohlcv_from": sec.ohlcv_src.name,
            "engine": strategy,
        },
        "validation": {"monte_carlo": {"n_simulations": MC_SIMS}},
        "_run_card_effective_sources": ["local_artifacts"],
    }
    (dst / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _run(run_dir: Path, data_map: dict, expect_confirm: list[str]) -> dict:
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
    for aid in cfg.get("alphas") or []:
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
    if list(getattr(sig, "confirm_ids", [])) != list(expect_confirm):
        raise SystemExit(
            f"confirm_ids mismatch: got {getattr(sig, 'confirm_ids', None)} "
            f"expect {expect_confirm}"
        )

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
        "confirm_ids": list(getattr(sig, "confirm_ids", [])),
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


def _maybe_published_f1(sec: SectorJob) -> dict | None:
    if not sec.published_f1 or not (sec.published_f1 / "artifacts" / "metrics.csv").exists():
        return None
    metrics = pd.read_csv(sec.published_f1 / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = sec.published_f1 / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {"metrics": metrics, "validation": validation, "run": sec.published_f1.name}


def _write_prop(sec: SectorJob, f0: dict, f1: dict, legacy: dict, irs: dict[str, float]) -> None:
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    d_ex_f1 = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sh_f1 = float(m1["sharpe"]) - float(m0["sharpe"])
    d_tot_f1 = float(m1["total_return"]) - float(m0["total_return"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    ir_txt = f"081:{irs['gtja191_081']:.3f}, 160:{irs['gtja191_160']:.3f}"
    winner = "F1" if d_ex_f1 > 0 else "F0"

    if winner == "F0":
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上的最优复现：
F0' = 动量 164 + 硬确认 081/160（与金属铜存活因子确认腿重搜结果对齐迁移）。
对照原基线 F0(171/054)：Δexcess={_pct(d_ex_legacy)}。
同池实测 F1'（081/160 + MAX 底部否决）未带来增量（Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}），故默认回退 F0'。
禁止为追收益叠加 custom 否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股{sec.name}相关板块（{sec.universe_desc}）
- 研究/回测参考区间：{sec.start_date} 至 {sec.end_date}（数据不足则用可得区间并说明）
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
- 3–5 条白话：相对基准、{sec.risk_note}
- 声明：研究/回测，非实盘建议

```
"""
    else:
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上的最优复现：
F1' = 动量 164 + 硬确认 081/160 + custom_max_inv 底部否决。
同池实测（run `{sec.f1_run.name}` vs `{sec.f0_run.name}`）：
- F1' 相对 F0'：Δtotal={_pct(d_tot_f1)}，Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}
- 确认腿与金属铜存活因子重搜结果对齐（081/160）；相对原基线 171/054 的 F0' Δexcess={_pct(d_ex_legacy)}。

本版只做一件事：在{sec.name}上复用 F0'(081/160) + custom_max_inv 底部否决，不改过程约束。
优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认；若不及同池 F0' 明文写「F1' 未在{sec.name}带来增量」。

【股票池与区间】
- 标的池：A股{sec.name}相关板块（{sec.universe_desc}）
- 研究/回测参考区间：{sec.start_date} 至 {sec.end_date}（数据不足则用可得区间并说明）
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
【禁止】gtja191_114 / 反转因子；禁止用 custom 替换 081/160；
禁止 custom_ivol/abn_to/vp_corr/comp_low/near_low 进硬确认或重排

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
- 因子 ID + 角色；说明 custom_max_inv 是反彩票底部否决；确认腿为 081/160
- 最近调仓日持仓表；完整指标 + MC p
- 必须对照同池 F0' 与原基线 171/054：给出 Δtotal / Δexcess / Δsharpe
- 3–5 条白话：{sec.risk_note}
- 声明：研究/回测，非实盘建议

```
"""
    sec.prop_out.write_text(body, encoding="utf-8")
    print(f"wrote prop {sec.prop_out} (winner={winner})")


def _write_reports(
    sec: SectorJob,
    f0: dict,
    f1: dict,
    legacy: dict,
    name_map: dict[str, str],
    irs: dict[str, float],
) -> str:
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    vl = (legacy.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(sec.f0_run, name_map)
    hold1_md, h1 = _holdings(sec.f1_run, name_map)
    pub = _maybe_published_f1(sec)

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    d_tot_legacy = float(m0["total_return"]) - float(ml["total_return"])
    d_sh_legacy = float(m0["sharpe"]) - float(ml["sharpe"])
    winner = "F1" if d_excess > 0 else "F0"

    eq = pd.read_csv(sec.f0_run / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    report_f0 = f"""# F0' 最优确认报告 — {sec.panel}（164 + 081/160）

**声明：研究/回测结果，非实盘建议。本报告为{sec.name}板块迁移金属铜确认腿重搜结果后的正式复跑：F0' = 164 + 081/160。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{sec.panel}** |
| 策略 | F0' = 动量 `gtja191_164` + 硬确认 `gtja191_081`/`gtja191_160` |
| F0' run | `{sec.f0_run.name}` |
| 同池 F1' 对照 | `{sec.f1_run.name}` |
| 原基线 F0 | `{sec.legacy_f0_run.name}`（171/054，同池重跑） |
| 成分来源 | {sec.universe_desc}；有效 OHLCV {f0['n_symbols']} |
| 配置区间 | {sec.start_date} → {sec.end_date} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `gtja191_164, gtja191_081, gtja191_160` |
| is_rebalance_days | {f0.get('is_rebalance_days')} |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_081 | F0' 硬确认（IR {_num(irs['gtja191_081'], 3)}） |
| gtja191_160 | F0' 硬确认（IR {_num(irs['gtja191_160'], 3)}） |

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
        f"- 确认腿自金属铜存活因子重搜迁移，相对原基线 171/054 有样本内过拟合风险"
        f"（Δexcess={_pct(d_ex_legacy)}）。\n"
    )

    pub_ex = _pct(pub["metrics"]["excess_return"]) if pub else "n/a"
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
    if pub:
        report_f0 += f"- 已发布旧 F1(171/054+MAX) `{pub['run']}` excess={pub_ex}（结构不同，仅供参照）。\n\n"

    if d_excess > 0:
        report_f0 += f"> **对照：同池 F1' excess 高于 F0'**（Δexcess={_pct(d_excess)}）。本板块最优为 F1'。\n"
    else:
        report_f0 += (
            f"> **结论：本板块以 F0'（081/160）为准**；"
            f"F1' 未带来增量（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）；"
            f"相对原基线 171/054 的 Δexcess={_pct(d_ex_legacy)}。\n"
        )

    report_f0 += f"""
## 5. 最近调仓日持仓（F0'）

- 日期：{h0['last_date']}；持仓 {h0['n_held']} 只；权重和 {_num(h0['gross'])}
- 有仓日平均持仓只数 {_num(h0['mean_n'], 2)}（中位 {_num(h0['median_n'], 2)}）

{hold0_md}

## 6. 白话结论

1. F0' = 纯动量 164 + 确认腿 081/160（与金属铜重搜结果对齐迁移）；过程约束不变。
2. 相对原基线 171/054 的 Δexcess / Δsharpe 是迁移是否有效的第一判据。
3. 同池 MAX 否决（F1'）无增量时回退 F0'，禁止为追收益扩因子。
4. {sec.risk_note}。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{sec.f0_run.resolve()}`。
"""

    report_f1 = f"""# F1' MAX否决报告 — {sec.panel}（081/160 + max_inv）

**声明：研究/回测结果，非实盘建议。F1' = F0'(164+081/160) + `custom_max_inv` 底部 20% 否决。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{sec.panel}** |
| 策略 | F1' = F0'(164+081/160) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1' run | `{sec.f1_run.name}` |
| 同池 F0' 对照 | `{sec.f0_run.name}` |
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
            f"> **同板块结论：F1' 未在{sec.name}带来增量**"
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
2. 若相对 F0' 无增量，回退 081/160 双确认。
3. {sec.risk_note}。
4. 研究/回测，非实盘建议。

---

本 run artifacts：`{sec.f1_run.resolve()}`。
"""

    (sec.f0_run / f"REPORT_F0_{sec.key}_081_160.md").write_text(report_f0, encoding="utf-8")
    (sec.f1_run / f"REPORT_F1_{sec.key}_081_160.md").write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            sec.f0_run,
            f"F0'最优确认-{sec.name}-081/160",
            f"请用国泰君安191做F0'多因子选股并回测：{sec.panel}，动量164+确认081/160",
        ),
        (
            sec.f1_run,
            f"F1' MAX否决-{sec.name}-081/160",
            f"请用国泰君安191+custom_max_inv做F1'多因子选股并回测：{sec.panel}，确认081/160",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {
                        "panel": sec.panel,
                        "confirm_ids": CONFIRM_NEW,
                        "ohlcv_from": sec.ohlcv_src.name,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    _write_prop(sec, f0, f1, legacy, irs)
    print(f"[{sec.key}] wrote reports; winner={winner}")
    return winner


def _run_sector(sec: SectorJob, irs: dict[str, float]) -> str:
    print("=" * 72)
    print(f"[sector] {sec.name} / {sec.panel}")
    if not sec.ohlcv_src.exists():
        raise SystemExit(f"missing OHLCV source: {sec.ohlcv_src}")
    if not ENGINE_F0_SRC.exists() or not ENGINE_F1_SRC.exists():
        raise SystemExit("missing F0/F1 engine templates")

    data_map = _load_ohlcv(sec.ohlcv_src)
    names = _name_map(sec)
    print(f"[panel] symbols={len(data_map)} names={len(names)}")

    print("-" * 40, "legacy F0 171/054")
    _prepare(
        sec.legacy_f0_run,
        sec,
        data_map,
        ENGINE_F0_SRC,
        CONFIRM_LEGACY,
        irs,
        f"F0_{sec.key}_legacy171054",
    )
    legacy = _run(sec.legacy_f0_run, data_map, CONFIRM_LEGACY)
    print("legacy excess", legacy["metrics"].get("excess_return"))

    print("-" * 40, "F0' 081+160")
    _prepare(
        sec.f0_run,
        sec,
        data_map,
        ENGINE_F0_SRC,
        CONFIRM_NEW,
        irs,
        f"F0_{sec.key}_081_160",
    )
    f0 = _run(sec.f0_run, data_map, CONFIRM_NEW)
    print("F0' excess", f0["metrics"].get("excess_return"))

    print("-" * 40, "F1' 081+160+max")
    _prepare(
        sec.f1_run,
        sec,
        data_map,
        ENGINE_F1_SRC,
        CONFIRM_NEW,
        irs,
        f"F1_{sec.key}_081_160",
    )
    f1 = _run(sec.f1_run, data_map, CONFIRM_NEW)
    print("F1' excess", f1["metrics"].get("excess_return"))

    return _write_reports(sec, f0, f1, legacy, names, irs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=("robot", "memory"), default=None)
    args = ap.parse_args()

    irs = _load_irs()
    print(
        f"[ir] 081={irs['gtja191_081']:.4f} 160={irs['gtja191_160']:.4f} "
        f"171={irs['gtja191_171']:.4f} 054={irs['gtja191_054']:.4f}"
    )

    jobs = [s for s in SECTORS if args.only is None or s.key == args.only]
    winners = {}
    for sec in jobs:
        winners[sec.key] = _run_sector(sec, irs)

    print("[done]", winners)


if __name__ == "__main__":
    main()
