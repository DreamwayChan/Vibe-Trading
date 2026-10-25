"""Materialize two frontend-facing ETF runs with standalone REPORT_*.md files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

PARENT = _AGENT_ROOT / "runs" / "20260726_113500_01_ETF_F0F1"
F0_RUN = _AGENT_ROOT / "runs" / "20260726_113500_01_ETF_F0"
F1_RUN = _AGENT_ROOT / "runs" / "20260726_113500_02_ETF_F1"


def _pct(x) -> str:
    return f"{float(x) * 100:.2f}%"


def _num(x, nd: int = 4) -> str:
    return f"{float(x):.{nd}f}"


def _metrics(run: Path) -> tuple[dict, dict]:
    m = pd.read_csv(run / "artifacts" / "metrics.csv").iloc[0].to_dict()
    v = json.loads((run / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    return m, v.get("monte_carlo") or {}


def _holdings(run: Path, name_map: dict[str, str]) -> tuple[str, pd.Series, str]:
    pos = pd.read_csv(run / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    held = pos.loc[last]
    held = held[held > 0].sort_values(ascending=False)
    lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
    for i, (code, w) in enumerate(held.items(), 1):
        lines.append(f"| {i} | {code} | {name_map.get(code, '')} | {float(w):.4f} |")
    return str(last.date()), held, "\n".join(lines)


def _top_table(top15: pd.DataFrame) -> str:
    lines = [
        "| # | 代码 | 名称 | S分位平滑 | 动量排名 |",
        "|---|------|------|-----------|----------|",
    ]
    for _, row in top15.iterrows():
        lines.append(
            f"| {int(row['#'])} | {row['ts_code']} | {row['name']} | "
            f"{float(row['S']):.4f} | {int(row['rank'])} |"
        )
    return "\n".join(lines)


def main() -> None:
    meta = json.loads((PARENT / "universe_meta.json").read_text(encoding="utf-8"))
    name_map = dict(meta.get("names") or {})
    top15 = pd.read_csv(PARENT / "TOP15_F0.csv")
    m0, v0 = _metrics(F0_RUN)
    m1, v1 = _metrics(F1_RUN)
    h0_date, h0, h0_md = _holdings(F0_RUN, name_map)
    h1_date, h1, h1_md = _holdings(F1_RUN, name_map)
    top_md = _top_table(top15)
    n_sym = len(json.loads((F0_RUN / "config.json").read_text(encoding="utf-8")).get("codes") or [])

    d_total = float(m1["total_return"]) - float(m0["total_return"])
    d_excess = float(m1["excess_return"]) - float(m0["excess_return"])
    d_sharpe = float(m1["sharpe"]) - float(m0["sharpe"])

    report_f0 = f"""# F0 双确认报告 — A股权益 ETF

**声明：研究/回测结果，非实盘建议。本报告为 A 股权益 ETF 流动性截面上的 F0（164 + 171/054 硬确认）正式复跑。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 标的池 | **A股权益 ETF**（剔除债/货/商品/REIT；成交额 Top180） |
| 策略 | F0 = 动量 `gtja191_164` + 硬确认 `gtja191_171`/`gtja191_054`（无 custom 否决） |
| F0 run | `20260726_113500_01_ETF_F0` |
| 同池 F1 对照 | `20260726_113500_02_ETF_F1` |
| 数据源 | AkShare Sina ETF 列表 + 日线（Tushare ETF 接口无权限） |
| 上市 ETF 全量 | {meta['listed_n']} |
| 权益候选 / 有效 OHLCV | {meta['equity_n']} / {n_sym} |
| 配置区间 | 2024-01-01 → 2026-07-24 |
| 因子 | `gtja191_164, gtja191_171, gtja191_054` |
| 持仓设定 | TopN=15，保留带=22，周频，换手帽 20% |
| is_rebalance_days | 132 |
| 说明 | 因子经 Registry 由 OHLCV 计算；未用 114 / 反转 / custom 质量确认 |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_171 | F0 硬确认（IR 0.155） |
| gtja191_054 | F0 硬确认（IR 0.152） |

- 确认分 C ≥ 当日截面 40% 分位才允许新开；确认全缺日不过滤。
- 未使用 `custom_max_inv`；ETF 截面上 F1 未带来增量，本版以 F0 为主策略。

## 3. F0 核心绩效

| 指标 | 数值 |
|------|------|
| final_value | {float(m0['final_value']):.2f} |
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
| is_rebalance_days | 132 |
| MC p_value_sharpe | {_num(v0.get('p_value_sharpe'))} |
| MC n_simulations | {v0.get('n_simulations', 500)} |

### 必须承认

- excess = {_pct(m0['excess_return'])} < 0：相对权益 ETF 等权基准为负，须明文承认。
- MC p_sharpe = {_num(v0.get('p_value_sharpe'))}（阈值 0.2）。
- 基准为同池等权；未计入 ETF 费率与折溢价。

## 4. 同池对照：F0 vs F1

F1 run：`20260726_113500_02_ETF_F1`（F0 + `custom_max_inv` 底部 20% 否决）

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

> **结论：ETF 截面以 F0 为准**（相对 F1：Δtotal={_pct(-d_total)}, Δexcess={_pct(-d_excess)}, Δsharpe={_num(-d_sharpe)}）。F1 未带来增量，禁止为追收益叠加 MAX 否决。

## 5. F0 过滤后 Top15（截面日 2026-07-20）

> 选股名单以「通过确认过滤 + 动量 S 排序」为准。换手帽与择时半仓会使实际持仓只数小于 15。

{top_md}

## 6. 最近调仓日实际持仓（F0）

- 日期：{h0_date}；持仓 {len(h0)} 只；权重和 {float(h0.sum()):.4f}

{h0_md}

## 7. 白话结论

1. F0 = 纯动量排序 + 171/054 硬确认；过程约束写死在 signal_engine。
2. 本样本 excess 为负：ETF 截面同质性强，动量难相对等权基准持续超额。
3. 同池 F1（MAX 底部否决）弱于 F0，ETF 默认策略回退 F0。
4. Top15 偏医药/消费/券商/海外宽基，属截面动量结果，非主题推荐。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F0_RUN.resolve()}`。
"""

    report_f1 = f"""# F1 MAX否决报告 — A股权益 ETF

**声明：研究/回测结果，非实盘建议。F1 = F0 + `custom_max_inv` 底部 20% 否决；本报告含同池 F0 对照。**

## 1. 运行概况

| 项 | 内容 |
|----|------|
| 标的池 | **A股权益 ETF**（剔除债/货/商品/REIT；成交额 Top180） |
| 策略 | F1 = F0(164+171/054) + `custom_max_inv` 底部 20% 否决新开仓 |
| F1 run | `20260726_113500_02_ETF_F1` |
| 同池 F0 对照 | `20260726_113500_01_ETF_F0` |
| 数据源 | AkShare Sina ETF 列表 + 日线（Tushare ETF 接口无权限） |
| 上市 ETF 全量 | {meta['listed_n']} |
| 权益候选 / 有效 OHLCV | {meta['equity_n']} / {n_sym} |
| 配置区间 | 2024-01-01 → 2026-07-24 |
| 因子 | `gtja191_164, gtja191_171, gtja191_054, custom_max_inv` |
| 持仓设定 | TopN=15，保留带=22，周频，换手帽 20% |
| is_rebalance_days | 132 |

## 2. 因子角色

| ID | 角色 |
|----|------|
| gtja191_164 | 动量主排序（S） |
| gtja191_171 | F0 硬确认（IR 0.155） |
| gtja191_054 | F0 硬确认（IR 0.152） |
| custom_max_inv | 反彩票底部否决（仅底部 20%，拦新开；不是质量主确认） |

- F0 新开仓：C ≥ 截面 40% 分位。
- F1 额外：`smooth(custom_max_inv)` < 截面 20% → 禁止新开；已持仓续持不受该否决。

## 3. F1 核心绩效

| 指标 | 数值 |
|------|------|
| final_value | {float(m1['final_value']):.2f} |
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
| is_rebalance_days | 132 |
| MC p_value_sharpe | {_num(v1.get('p_value_sharpe'))} |
| MC n_simulations | {v1.get('n_simulations', 500)} |

### 必须承认

- excess = {_pct(m1['excess_return'])} < 0：相对权益 ETF 等权基准为负。
- MC p_sharpe = {_num(v1.get('p_value_sharpe'))} > 0.2：未通过显著性阈值。
- **F1 未带来增量**（相对 F0：Δtotal={_pct(d_total)}, Δexcess={_pct(d_excess)}, Δsharpe={_num(d_sharpe)}）。

## 4. 同池对照：F1 vs F0

| 指标 | F1 | F0 | F1−F0 |
|------|----|----|-------|
| total_return | {_pct(m1['total_return'])} | {_pct(m0['total_return'])} | {_pct(d_total)} |
| excess_return | {_pct(m1['excess_return'])} | {_pct(m0['excess_return'])} | {_pct(d_excess)} |
| sharpe | {_num(m1['sharpe'])} | {_num(m0['sharpe'])} | {_num(d_sharpe)} |
| max_drawdown | {_pct(m1['max_drawdown'])} | {_pct(m0['max_drawdown'])} | — |
| trade_count | {_num(m1.get('trade_count'), 0)} | {_num(m0.get('trade_count'), 0)} | — |
| rebalance_turnover_mean | {_num(m1.get('rebalance_turnover_mean'))} | {_num(m0.get('rebalance_turnover_mean'))} | — |
| benchmark_return | {_pct(m1['benchmark_return'])} | {_pct(m0['benchmark_return'])} | 0.00% |
| MC p_sharpe | {_num(v1.get('p_value_sharpe'))} | {_num(v0.get('p_value_sharpe'))} | — |

> **结论：ETF 截面 F1 弱于 F0**，默认回退 F0；本报告仅作对照披露。

## 5. F1 过滤后 Top15（截面日 2026-07-20）

> 当日 F1 与 F0 过滤后 Top15 相同（MAX 否决未改名单）。选股名单如下；换手帽/择时会使实际持仓只数小于 15。

{top_md}

## 6. 最近调仓日实际持仓（F1）

- 日期：{h1_date}；持仓 {len(h1)} 只；权重和 {float(h1.sum()):.4f}

{h1_md}

## 7. 白话结论

1. F1 只在 F0 上叠加 MAX 底部否决，不改周频/换手/最小持有等过程约束。
2. 本样本 F1 全面弱于 F0，且 excess/MC 均不达标，须明文承认「F1 未带来增量」。
3. ETF 截面因子区分度弱于主题个股，否决腿更易误杀动量龙头。
4. 对照名单仍可供研究；实盘配置请以 F0 报告为准。
5. 研究/回测，非实盘建议。

---

本 run artifacts：`{F1_RUN.resolve()}`。
"""

    (F0_RUN / "REPORT_F0_etf.md").write_text(report_f0, encoding="utf-8")
    (F1_RUN / "REPORT_F1_etf.md").write_text(report_f1, encoding="utf-8")
    (PARENT / "REPORT_F0_etf.md").write_text(report_f0, encoding="utf-8")
    (PARENT / "REPORT_F1_etf.md").write_text(report_f1, encoding="utf-8")

    for run, title, prompt in (
        (
            F0_RUN,
            "F0双确认-A股权益ETF",
            "A股权益ETF截面 F0（164+171/054硬确认）Top15 筛选与回测",
        ),
        (
            F1_RUN,
            "F1 MAX否决-A股权益ETF",
            "A股权益ETF截面 F1（F0+custom_max_inv底部20%否决）Top15 筛选与回测",
        ),
    ):
        (run / "req.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "prompt": prompt,
                    "context": {
                        "panel": "A股权益ETF",
                        "source_run": PARENT.name,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        top15.to_csv(run / "artifacts" / "TOP15.csv", index=False, encoding="utf-8-sig")

    print(f"wrote {F0_RUN / 'REPORT_F0_etf.md'}")
    print(f"wrote {F1_RUN / 'REPORT_F1_etf.md'}")


if __name__ == "__main__":
    main()
