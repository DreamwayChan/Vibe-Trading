"""Formal rerun of researched optimal confirms on robot / memory / foodbev.

Strategies (from alive-factor confirm search):
  - 人形机器人: F0' = 164 + 124/178
  - 存储芯片:   F0' = 164 + 185/070
  - 食品饮料:   F0' = 164 + 127
  - 算力硬件:   F0' = 164 + 178/156
  - 创新药:     F0' = 164 + 129
  - 其他专用设备: F0' = 164 + 127/111
  - AI应用:     F0' = 164 + 171
  - 工业母机:   F0' = 164 + 171/054（重搜后基线仍最优）
  - 虚拟现实:   F0' = 164 + 077
  - 其他汽车零部件: F0' = 164 + 100/081

Also runs F1' (MAX veto) and legacy F0 (171/054) on the same OHLCV,
writes REPORT_*.md + req.json, refreshes prop summary sections.

Run from agent/:
    python scripts/run_sectors_optimal_researched.py
    python scripts/run_sectors_optimal_researched.py --only autoparts
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from scripts.research_copper_confirm_search import MOM_ID, _smooth_cs  # noqa: E402
from scripts.research_sector_confirm_search import (  # noqa: E402
    _build_panel,
    _full_run,
    _load_alive_irs,
    _load_ohlcv,
    _name_map,
    _pct,
    _num,
)
from src.factors.registry import Registry  # noqa: E402

MC_SIMS = 1000
CONFIRM_LEGACY = ["gtja191_171", "gtja191_054"]


@dataclass(frozen=True)
class OptimalJob:
    key: str
    name: str
    panel: str
    universe_desc: str
    ohlcv_src: Path
    start_date: str
    end_date: str
    initial_cash: float
    risk_note: str
    confirm_ids: tuple[str, ...]
    amount_note: str
    f0_run: Path
    f1_run: Path
    legacy_f0_run: Path
    prop_out: Path
    published_f1: Path | None
    # Minimal Sector-like shim for _full_run / _name_map
    out_root: Path

    @property
    def confirm_label(self) -> str:
        return "+".join(a.replace("gtja191_", "") for a in self.confirm_ids)


# Adapt Sector duck-typing expected by research helpers.
class _SecView:
    def __init__(self, job: OptimalJob):
        self.key = job.key
        self.name = job.name
        self.panel = job.panel
        self.universe_desc = job.universe_desc
        self.ohlcv_src = job.ohlcv_src
        self.start_date = job.start_date
        self.end_date = job.end_date
        self.initial_cash = job.initial_cash
        self.risk_note = job.risk_note
        self.out_root = job.out_root
        self.prop_out = job.prop_out


JOBS: list[OptimalJob] = [
    OptimalJob(
        key="robot",
        name="人形机器人",
        panel="人形机器人 BK1184",
        universe_desc="东财概念 人形机器人 BK1184（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=10_000_000,
        risk_note="主题拥挤、订单兑现与估值波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_124", "gtja191_178"),
        amount_note="gtja191_124 需要 amount，允许 close×volume 合成；禁止为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260725_184000_05_F0_robot_124_178",
        f1_run=_AGENT_ROOT / "runs" / "20260725_184000_06_F1_robot_124_178",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260725_184000_05_F0_robot_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_人形机器人_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
        out_root=_AGENT_ROOT / "runs" / "_formal_robot_124_178",
    ),
    OptimalJob(
        key="memory",
        name="存储芯片",
        panel="存储芯片 BK1137",
        universe_desc="东财 存储芯片 BK1137（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe",
        start_date="2024-01-01",
        end_date="2026-07-31",
        initial_cash=1_000_000,
        risk_note="存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_185", "gtja191_070"),
        amount_note="gtja191_070 需要 amount，允许 close×volume 合成；禁止为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_05_F0_memory_185_070",
        f1_run=_AGENT_ROOT / "runs" / "20260731_tech3_06_F1_memory_185_070",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_05_F0_memory_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_存储芯片_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260731_tech3_02_F1_memory",
        out_root=_AGENT_ROOT / "runs" / "_formal_memory_185_070",
    ),
    OptimalJob(
        key="foodbev",
        name="食品饮料",
        panel="食品饮料 BK0438",
        universe_desc="东财行业「食品饮料」BK0438（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_foodbev_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="白酒估值分化、消费景气与成本周期；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_127",),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_foodbev_127",
        f1_run=_AGENT_ROOT / "runs" / "20260730_162800_04_F1_foodbev_127",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_foodbev_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_食品饮料_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260730_162800_02_F1_foodbev",
        out_root=_AGENT_ROOT / "runs" / "_formal_foodbev_127",
    ),
    OptimalJob(
        key="compute",
        name="算力硬件",
        panel="算力硬件（光模块∪PCB∪液冷）",
        universe_desc="东财 光通信模块 BK1136 ∪ 印制电路板 BK1340 ∪ 液冷概念 BK1138（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260731_tech3_00_compute_universe",
        start_date="2024-01-01",
        end_date="2026-07-31",
        initial_cash=1_000_000,
        risk_note="算力链景气、光模块/PCB/液冷主题轮动与估值波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_178", "gtja191_156"),
        amount_note="gtja191_156 需要 amount，允许 close×volume 合成；禁止为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_05_F0_compute_178_156",
        f1_run=_AGENT_ROOT / "runs" / "20260731_tech3_06_F1_compute_178_156",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260731_tech3_05_F0_compute_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_算力硬件_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260731_tech3_02_F1_compute",
        out_root=_AGENT_ROOT / "runs" / "_formal_compute_178_156",
    ),
    OptimalJob(
        key="drug",
        name="创新药",
        panel="创新药 BK1106",
        universe_desc="东财概念「创新药」BK1106 ∪ 原面板（排除北交所；含 300142.SZ）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260807_drug_00_universe",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=10_000_000,
        risk_note="创新药政策/临床/估值波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_129",),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260725_184000_05_F0_drug_129",
        f1_run=_AGENT_ROOT / "runs" / "20260725_184000_06_F1_drug_129",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260725_184000_05_F0_drug_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_创新药_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260725_184000_02_F1_drug",
        out_root=_AGENT_ROOT / "runs" / "_formal_drug_129",
    ),
    OptimalJob(
        key="speceq",
        name="其他专用设备",
        panel="其他专用设备 BK1405",
        universe_desc="东财行业「其他专用设备」BK1405（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_speceq_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="设备订单/资本开支周期与板块轮动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_127", "gtja191_111"),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_speceq_127_111",
        f1_run=_AGENT_ROOT / "runs" / "20260730_162800_04_F1_speceq_127_111",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_speceq_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_其他专用设备_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260730_162800_02_F1_speceq",
        out_root=_AGENT_ROOT / "runs" / "_formal_speceq_127_111",
    ),
    OptimalJob(
        key="aiapp",
        name="AI应用",
        panel="AI应用",
        universe_desc="A股 AI 应用相关标的池（沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260725_193311_93_84c623",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="AI 应用主题轮动、业绩兑现差与高波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_171",),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260725_193311_05_F0_aiapp_171",
        f1_run=_AGENT_ROOT / "runs" / "20260725_193311_06_F1_aiapp_171",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260725_193311_05_F0_aiapp_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_AI应用_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260725_193311_93_84c623",
        out_root=_AGENT_ROOT / "runs" / "_formal_aiapp_171",
    ),
    OptimalJob(
        key="machine",
        name="工业母机",
        panel="工业母机 BK1004",
        universe_desc="东财概念 工业母机 BK1004（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260726_084500_01_F0_machine",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="工业母机订单/设备周期与国产替代叙事波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_171", "gtja191_054"),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260726_084500_03_F0_machine_171_054",
        f1_run=_AGENT_ROOT / "runs" / "20260726_084500_04_F1_machine_171_054",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260726_084500_03_F0_machine_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_工业母机_最优.md",
        published_f1=None,
        out_root=_AGENT_ROOT / "runs" / "_formal_machine_171_054",
    ),
    OptimalJob(
        key="vrar",
        name="虚拟现实",
        panel="虚拟现实 BK0722",
        universe_desc="东财概念「虚拟现实」BK0722（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260726_123500_00_VRAR_universe",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="VR/AR 产品周期、消费电子景气与主题炒作波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_077",),
        amount_note="gtja191_077 需要 amount，允许 close×volume 合成；禁止为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260726_123500_03_F0_vrar_077",
        f1_run=_AGENT_ROOT / "runs" / "20260726_123500_04_F1_vrar_077",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260726_123500_03_F0_vrar_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_虚拟现实_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260726_123500_02_F1_vrar",
        out_root=_AGENT_ROOT / "runs" / "_formal_vrar_077",
    ),
    OptimalJob(
        key="autoparts",
        name="其他汽车零部件",
        panel="其他汽车零部件 BK1528",
        universe_desc="东财行业「其他汽车零部件」BK1528（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_autoparts_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="汽车产业链景气、新能源渗透与零部件价格波动；确认腿样本内重搜有过拟合风险",
        confirm_ids=("gtja191_100", "gtja191_081"),
        amount_note="不要为 114 拼 amount",
        f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_autoparts_100_081",
        f1_run=_AGENT_ROOT / "runs" / "20260730_162800_04_F1_autoparts_100_081",
        legacy_f0_run=_AGENT_ROOT / "runs" / "20260730_162800_03_F0_autoparts_legacy171054",
        prop_out=_AGENT_ROOT / "prop_国泰君安_其他汽车零部件_最优.md",
        published_f1=_AGENT_ROOT / "runs" / "20260730_162800_01_F0_autoparts",
        out_root=_AGENT_ROOT / "runs" / "_formal_autoparts_100_081",
    ),
]


def _precompute_needed(
    data_map: dict[str, pd.DataFrame], need: list[str]
) -> dict[str, pd.DataFrame]:
    panel = _build_panel(data_map)
    reg = Registry()
    scores: dict[str, pd.DataFrame] = {}
    for aid in need:
        print(f"  precompute {aid}…")
        raw = reg.compute(aid, panel)
        if raw is None or raw.empty:
            raise SystemExit(f"factor {aid} empty")
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = _smooth_cs(raw)
        print(f"    non_null={int(scores[aid].notna().to_numpy().sum())}")
    return scores


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


def _maybe_pub(path: Path | None) -> dict | None:
    if not path or not (path / "artifacts" / "metrics.csv").exists():
        return None
    metrics = pd.read_csv(path / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = path / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {"metrics": metrics, "validation": validation, "run": path.name}


def _write_reports(
    job: OptimalJob,
    f0: dict,
    f1: dict,
    legacy: dict,
    irs: dict[str, float],
    name_map: dict[str, str],
) -> str:
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    v1 = (f1.get("validation") or {}).get("monte_carlo", {})
    vl = (legacy.get("validation") or {}).get("monte_carlo", {})
    hold0_md, h0 = _holdings(job.f0_run, name_map)
    hold1_md, h1 = _holdings(job.f1_run, name_map)
    pub = _maybe_pub(job.published_f1)

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    d_tot_legacy = float(m0["total_return"]) - float(ml["total_return"])
    d_sh_legacy = float(m0["sharpe"]) - float(ml["sharpe"])
    winner = "F1" if d_excess > 0 else "F0"
    conf = job.confirm_label

    eq = pd.read_csv(job.f0_run / "artifacts" / "equity.csv")
    date_col = "timestamp" if "timestamp" in eq.columns else eq.columns[0]
    actual_start = str(pd.to_datetime(eq[date_col].iloc[0]).date())
    actual_end = str(pd.to_datetime(eq[date_col].iloc[-1]).date())

    ir_rows = "\n".join(
        f"| {aid} | F0' 硬确认（IR {_num(irs.get(aid, 0.0), 3)}） |" for aid in job.confirm_ids
    )

    report_f0 = f"""# F0' 最优确认报告 — {job.panel}（164 + {conf}）

**声明：研究/回测结果，非实盘建议。本报告为{job.name}板块在 GTJA191 存活因子自由组合重搜后的最优正式复跑：F0' = 164 + {conf}。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{job.panel}** |
| 策略 | F0' = 动量 `gtja191_164` + 硬确认 `{'`/`'.join(job.confirm_ids)}` |
| F0' run | `{job.f0_run.name}` |
| 同池 F1' 对照 | `{job.f1_run.name}` |
| 原基线 F0 | `{job.legacy_f0_run.name}`（171/054，同池重跑） |
| 成分来源 | {job.universe_desc}；有效 OHLCV {f0.get('n_symbols', 'n/a')} |
| 配置区间 | {job.start_date} → {job.end_date} |
| 实际数据 | {actual_start} → {actual_end} |
| 因子 | `{', '.join([MOM_ID] + list(job.confirm_ids))}` |
| amount | {job.amount_note} |
| is_rebalance_days | {f0.get('is_rebalance_days')} |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
{ir_rows}

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
        f"- 确认腿来自存活因子样本内重搜，相对原基线 171/054 Δexcess={_pct(d_ex_legacy)}，有过拟合风险。\n"
    )

    pub_ex = _pct(pub["metrics"]["excess_return"]) if pub else "n/a"
    report_f0 += f"""
## 4. 对照：F0' vs 原基线 171/054 vs F1'

| 指标 | F0' ({conf}) | 原 F0 (171/054) | F1' ({conf}+MAX) |
|------|-------------|-----------------|------------------|
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
            f"> **结论：本板块以 F0'（{conf}）为准**；"
            f"F1' 未带来增量（Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）；"
            f"相对原基线 171/054 的 Δexcess={_pct(d_ex_legacy)}。\n"
        )

    report_f0 += f"""
## 5. 最近调仓日持仓（F0'）

- 日期：{h0['last_date']}；持仓 {h0['n_held']} 只；权重和 {_num(h0['gross'])}
- 有仓日平均持仓只数 {_num(h0['mean_n'], 2)}（中位 {_num(h0['median_n'], 2)}）

{hold0_md}

## 6. 白话结论

1. F0' = 纯动量 164 + 本板块重搜确认腿 {conf}；周频 / 换手帽 / 最小持有 / 择时不变。
2. 相对原基线 171/054 的 Δexcess / Δsharpe 是迁移是否有效的第一判据。
3. 同池 MAX 否决（F1'）无增量时回退 F0'，禁止为追收益扩因子。
4. {job.risk_note}。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{job.f0_run.resolve()}`。
"""

    report_f1 = f"""# F1' MAX否决报告 — {job.panel}（{conf} + max_inv）

**声明：研究/回测结果，非实盘建议。F1' = F0'(164+{conf}) + `custom_max_inv` 底部 20% 否决。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 板块 | **{job.panel}** |
| 策略 | F1' = F0'(164+{conf}) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1' run | `{job.f1_run.name}` |
| 同池 F0' 对照 | `{job.f0_run.name}` |
| 因子 | `{', '.join([MOM_ID] + list(job.confirm_ids) + ['custom_max_inv'])}` |
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
            f"> **同板块结论：F1' 未在{job.name}带来增量**"
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
2. 若相对 F0' 无增量，回退 {conf} 双确认。
3. {job.risk_note}。
4. 研究/回测，非实盘建议。

---

本 run artifacts：`{job.f1_run.resolve()}`。
"""

    f0_report = job.f0_run / f"REPORT_F0_{job.key}_{conf.replace('+','')}.md"
    f1_report = job.f1_run / f"REPORT_F1_{job.key}_{conf.replace('+','')}.md"
    f0_report.write_text(report_f0, encoding="utf-8")
    f1_report.write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            job.f0_run,
            f"F0'最优确认-{job.name}-{conf}",
            f"请用国泰君安191做F0'多因子选股并回测：{job.panel}，动量164+确认{conf}",
        ),
        (
            job.f1_run,
            f"F1' MAX否决-{job.name}-{conf}",
            f"请用国泰君安191+custom_max_inv做F1'多因子选股并回测：{job.panel}，确认{conf}",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {
                        "panel": job.panel,
                        "confirm_ids": list(job.confirm_ids),
                        "ohlcv_from": job.ohlcv_src.name,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    _refresh_prop_summary(job, f0, f1, legacy, winner, h0, hold0_md, irs)
    print(f"[{job.key}] wrote {f0_report.name} / {f1_report.name}; winner={winner}")
    return winner


def _refresh_prop_summary(
    job: OptimalJob,
    f0: dict,
    f1: dict,
    legacy: dict,
    winner: str,
    h0: dict,
    hold0_md: str,
    irs: dict[str, float],
) -> None:
    """Keep the prompt body; refresh the trailing formal-summary block."""
    m0, m1, ml = f0["metrics"], f1["metrics"], legacy["metrics"]
    v0 = (f0.get("validation") or {}).get("monte_carlo", {})
    d_ex_f1 = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sh_f1 = float(m1["sharpe"]) - float(m0["sharpe"])
    d_ex_legacy = float(m0["excess_return"]) - float(ml["excess_return"])
    conf = job.confirm_label
    kind = "F1" if winner == "F1" else "F0"
    win_m = m1 if winner == "F1" else m0
    win_mc = (f1.get("validation") or {}).get("monte_carlo", {}) if winner == "F1" else v0

    # Rebuild prop from template so it stays consistent with formal numbers.
    ids = list(job.confirm_ids)
    ir_weighted = ", ".join(
        f"{aid.replace('gtja191_', '')}:{float(irs.get(aid, 0.0)):.3f}" for aid in ids
    )
    id_rows = "\n".join(
        [f"| {MOM_ID} | 动量（主排序，必须） |"]
        + [f"| {aid} | 确认（本板块重搜硬确认） |" for aid in ids]
        + (
            ["| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |"]
            if winner == "F1"
            else []
        )
    )
    copy_ids = ", ".join([MOM_ID] + ids + (["custom_max_inv"] if winner == "F1" else []))

    must = []
    if float(win_m["excess_return"]) < 0:
        must.append(f"- excess={_pct(win_m['excess_return'])}<0：未跑赢板块等权基准，必须明文承认。")
    else:
        must.append(
            f"- excess={_pct(win_m['excess_return'])}>0：本样本相对板块基准为正，仍须样本外复核。"
        )
    p = win_mc.get("p_value_sharpe")
    if p is not None and float(p) > 0.2:
        must.append(f"- MC p_sharpe={_num(p)}>0.2。")
    else:
        must.append(f"- MC p_sharpe={_num(p)}（阈值 0.2）。")
    must.append(
        f"- 相对原基线 171/054：Δexcess={_pct(d_ex_legacy if winner == 'F0' else float(m1['excess_return']) - float(ml['excess_return']))}；"
        f"确认腿为板块内存活因子重搜结果，有过拟合风险。"
    )

    if winner == "F0":
        header = f"""请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{job.name}板块（{job.universe_desc}）上，在 GTJA191 存活因子中自由组合重搜确认腿后的最优复现：
F0' = 动量 164 + 硬确认 {conf}。
同池 F0'(`{conf}`) vs F1'：Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}。
对照原基线 171/054：Δexcess={_pct(d_ex_legacy)}。
同池 F1' 未优于 F0' 时禁止叠加 custom 否决。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{chr(10).join(must)}"""
        n_core = 1 + len(ids)
        filter_extra = f"5. 面板 OHLCV；{job.amount_note}"
        ban = """【禁止】gtja191_114 / 反转因子；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排
【禁止】回退使用 171/054 作为本版确认腿（除非其完整引擎仍最优）"""
    else:
        header = f"""请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{job.name}板块（{job.universe_desc}）上，在 GTJA191 存活因子中自由组合重搜确认腿后的最优复现：
F1' = 动量 164 + 硬确认 {conf} + custom_max_inv 底部否决。
同池 F0' vs F1'：Δexcess={_pct(d_ex_f1)}，Δsharpe={_num(d_sh_f1)}。
对照原基线 171/054：Δexcess={_pct(float(m1['excess_return']) - float(ml['excess_return']))}。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{chr(10).join(must)}"""
        n_core = 2 + len(ids)
        filter_extra = f"""5. F1' 额外否决（仅新开仓）：smooth(custom_max_inv) < 当日截面 20% 分位 → 禁止新开
6. 面板 OHLCV；{job.amount_note}"""
        ban = """【禁止】gtja191_114 / 反转因子；禁止用 custom 替换确认腿"""

    body = f"""## 提示词

```
{header}

【股票池与区间】
- 标的池：A股{job.name}相关板块（{job.universe_desc}）
- 研究/回测参考区间：{job.start_date} 至 {job.end_date}（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 {n_core} 个）】
| ID | 角色 |
{id_rows}

复制用：{copy_ids}
{ban}

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权({ir_weighted})；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
{filter_extra}

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
- 因子 ID + 角色；确认腿为本板块存活因子重搜结果
- 最近调仓日持仓表；完整指标 + MC p
- 必须对照原基线 171/054：Δtotal / Δexcess / Δsharpe
- 3–5 条白话：{job.risk_note}
- 声明：研究/回测，非实盘建议

```

## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **{kind}** / `{conf}` |
| F0' run | `{job.f0_run.name}` |
| F1' run | `{job.f1_run.name}` |
| excess / sharpe / max_dd | {_pct(win_m['excess_return'])} / {_num(win_m['sharpe'])} / {_pct(win_m['max_drawdown'])} |
| MC p_sharpe | {_num(win_mc.get('p_value_sharpe'))} |
| Δexcess vs 171+054 | {_pct(float(win_m['excess_return']) - float(ml['excess_return']))} |
| 最近持仓日 | {h0['last_date']}（n={h0['n_held']}） |

{hold0_md}

研究/回测，非实盘建议。
"""
    job.prop_out.write_text(body, encoding="utf-8")
    print(f"refreshed prop {job.prop_out}")


def _run_job(job: OptimalJob, irs: dict[str, float]) -> str:
    print("=" * 72)
    print(f"[formal] {job.name} confirm={job.confirm_label}")
    sec = _SecView(job)
    job.out_root.mkdir(parents=True, exist_ok=True)

    data_map = _load_ohlcv(job.ohlcv_src)
    names = _name_map(sec)
    print(f"[panel] symbols={len(data_map)} names={len(names)}")

    need = [MOM_ID] + list(job.confirm_ids) + CONFIRM_LEGACY + ["custom_max_inv"]
    scores = _precompute_needed(data_map, need)

    print("-" * 40, "legacy F0 171/054")
    legacy = _full_run(
        sec, CONFIRM_LEGACY, scores, irs, data_map, job.legacy_f0_run,
        mc_sims=MC_SIMS, veto_max=False,
    )
    legacy["n_symbols"] = len(data_map)
    print("legacy excess", legacy["metrics"].get("excess_return"))

    print("-" * 40, f"F0' {job.confirm_label}")
    f0 = _full_run(
        sec, list(job.confirm_ids), scores, irs, data_map, job.f0_run,
        mc_sims=MC_SIMS, veto_max=False,
    )
    f0["n_symbols"] = len(data_map)
    print("F0' excess", f0["metrics"].get("excess_return"))

    print("-" * 40, f"F1' {job.confirm_label}+MAX")
    f1 = _full_run(
        sec, list(job.confirm_ids), scores, irs, data_map, job.f1_run,
        mc_sims=MC_SIMS, veto_max=True,
    )
    f1["n_symbols"] = len(data_map)
    print("F1' excess", f1["metrics"].get("excess_return"))

    return _write_reports(job, f0, f1, legacy, irs, names)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        choices=(
            "robot",
            "memory",
            "foodbev",
            "compute",
            "drug",
            "speceq",
            "aiapp",
            "machine",
            "vrar",
            "autoparts",
        ),
        default=None,
    )
    args = ap.parse_args()

    irs = _load_alive_irs()
    # Ensure researched confirms have IR entries.
    for job in JOBS:
        for aid in job.confirm_ids:
            irs.setdefault(aid, 0.10)
    for aid in CONFIRM_LEGACY + [MOM_ID]:
        irs.setdefault(aid, 0.10)

    print(
        f"[ir] 124={irs.get('gtja191_124')} 178={irs.get('gtja191_178')} "
        f"185={irs.get('gtja191_185')} 070={irs.get('gtja191_070')}"
    )

    jobs = [j for j in JOBS if args.only is None or j.key == args.only]
    winners = {}
    for job in jobs:
        winners[job.key] = _run_job(job, irs)
    print("[done]", winners)


if __name__ == "__main__":
    main()
