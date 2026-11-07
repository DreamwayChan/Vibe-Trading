"""Finalize BK1715 趋势股 deliverables: prop file, summary, run report.

Run from ``agent/``:
    python scripts/finalize_trendstock_report.py
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

from scripts.research_qlib158_trendstock_confirm_search import (  # noqa: E402
    _holdings_md,
    _name_map,
    _num,
    _pct,
)

RUN_ID = "20261103_204727_05_F0_trendstock_vsumn60__cord30_ksft"
RUN_DIR = _AGENT_ROOT / "runs" / RUN_ID
STAGE2 = _AGENT_ROOT / "runs" / "_research_qlib158_trendstock_stage2"
STAGE1 = _AGENT_ROOT / "runs" / "_research_qlib158_trendstock_confirm_search"
PROP = _AGENT_ROOT / "prop_qlib158_趋势股_最优.md"
SUPERSEDED = STAGE1 / "publish_stage1_superseded"
OLD_RUN = _AGENT_ROOT / "runs" / "20261103_203000_05_F0_trendstock_cord30_qtld30"

ROBUST = [
    ("2024-01-01 ~ 2025-06-30", "49.54%", "13.55%", "35.99%", "2.4493", "-6.85%"),
    ("2025-07-01 ~ 2026-11-03", "33.83%", "22.89%", "10.94%", "1.9217", "-8.30%"),
]


def main() -> None:
    m = pd.read_csv(RUN_DIR / "artifacts" / "metrics.csv").iloc[0].to_dict()
    card = json.loads((RUN_DIR / "run_card.json").read_text(encoding="utf-8"))
    mc = ((card.get("validation") or {}).get("monte_carlo") or {}).get("p_value_sharpe")
    names = _name_map()
    hold_md, hold_stats = _holdings_md(RUN_DIR, names)
    rank = pd.read_csv(STAGE2 / "stage2_rank.csv")
    ic = pd.read_csv(STAGE1 / "pool_ic.csv").sort_values("ir", ascending=False).head(10)

    robust_md = [
        "| 区间 | total | benchmark | excess | sharpe | max_dd | MC p_sharpe |",
        "|------|------:|----------:|-------:|-------:|-------:|------------:|",
    ]
    for row in ROBUST:
        robust_md.append(
            f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} | "
            + ("0.8800" if "2024" in row[0] else "0.7230") + " |"
        )
    robust_md = "\n".join(robust_md)

    ic_md = "\n".join(
        f"| {i} | `{r.id.replace('qlib158_', '')}` | {r.ic_mean:+.4f} | {r.ir:+.3f} | {r.pos_ratio:.2f} |"
        for i, r in enumerate(ic.itertuples(index=False), 1)
    )

    top_md = "\n".join(
        f"| {i} | {r.mom} | `{r.confirm}` | {_pct(r.excess_return)} | {_pct(r.total_return)} | "
        f"{_num(r.sharpe)} | {_pct(r.max_drawdown)} |"
        for i, r in enumerate(rank.head(10).itertuples(index=False), 1)
    )

    # --- run report -------------------------------------------------------
    report = f"""# 趋势股 BK1715 F0' — vsumn60 + cord30/ksft

**研究/回测，非实盘建议。**

## 来源

- 市场扫描（2026-11-03）排名第 2 的板块：**趋势股 BK1715**（概念板块，80 只成分，rs_share 67.5%，20 日 +18.87%，择时乘数 1.0）
- 过程参考同系列 F0'：金属铜 `kmid2+max60`、存储芯片 `kmid+ksft2` 的确认腿搜索流程
- 两段式搜索：快筛 352 组合 → 引擎排名 34 组 → 动量腿消融 5 条 → 蒙特卡洛复核

## 因子组合（本版 3 个）

| ID | 角色 | IR 权重 |
|----|------|--------:|
| `qlib158_vsumn60` | 主排序（60 日成交量下跌强度，卖压衰竭） | 0.178 |
| `qlib158_cord30` | 硬确认（30 日收盘-开盘相关性） | 0.128 |
| `qlib158_ksft` | 硬确认（开盘-收盘价差位置，K 线实体偏移） | 0.132 |

- 确认分 C = IR 加权(cord30, ksft)，新开仓要求 C ≥ 当日截面 40% 分位
- 已排除反转主题（cntn20/30/60、cntd60）；未使用吸筹/质量确认/custom_max_inv
- 同池 F1（+MAX 底部否决）Δexcess=-56.27%、Δsharpe=-1.04 → 回退 F0'，不叠加否决

## 指标（2024-01-01 ~ 2026-11-03，80 只成分，lake 本地日线）

| 指标 | 值 |
|------|----|
| total_return | {_pct(m.get('total_return'))} |
| annual_return | {_pct(m.get('annual_return'))} |
| benchmark_return（板块等权） | {_pct(m.get('benchmark_return'))} |
| excess_return | {_pct(m.get('excess_return'))} |
| sharpe | {_num(m.get('sharpe'))} |
| max_drawdown | {_pct(m.get('max_drawdown'))} |
| calmar / sortino | {_num(m.get('calmar'), 2)} / {_num(m.get('sortino'), 2)} |
| win_rate | {_pct(m.get('win_rate'))} |
| trade_count | {int(float(m.get('trade_count') or 0))} |
| rebalance_count | {int(float(m.get('rebalance_count') or 0))} |
| avg_holding_days | {_num(m.get('avg_holding_days'), 1)} |
| rebalance_turnover_mean | {_pct(m.get('rebalance_turnover_mean'))} |
| MC p_sharpe | {_num(mc)} |

## 与对照组

| 组合 | excess | total | sharpe | max_dd | 说明 |
|------|-------:|------:|-------:|-------:|------|
| **vsumn60 + cord30+ksft（本版）** | {_pct(m.get('excess_return'))} | {_pct(m.get('total_return'))} | {_num(m.get('sharpe'))} | {_pct(m.get('max_drawdown'))} | 最优 |
| std30 纯动量 | -22.95% | 17.37% | 0.8982 | -8.67% | 同池基准腿 |
| std30 + cord30+ksft | -10.35% | 29.97% | 1.5317 | -5.14% | 只换确认腿 |
| 金属铜 kmid2+max60 | -26.50% | 13.82% | 0.5496 | -10.62% | 参考板块组合在本池失效 |
| 存储芯片 kmid+ksft2 | -46.79% | -6.47% | -0.2456 | -17.74% | 参考板块组合在本池失效 |

结论：本池的超额主要来自**排序腿换成 vsumn60**，而非照搬参考板块的确认腿；金属铜/存储芯片的确认腿直接搬到趋势股是负超额。

## 稳健性

{robust_md}

- 两段区间 excess 都为正（+35.99% / +10.94%），说明不是单段行情的产物
- 收益集中度高：前 5 只贡献了毛收益的 86.26%（科森科技 28.72%、603278 24.15%、凡拓数创 14.25%），正贡献标的 39/80
- 最近持仓（{hold_stats['last_date']}，n={hold_stats['n_held']}）：

{hold_md}

## 必须承认

- MC p_sharpe={_num(mc)} > 0.2：相对随机交易的显著性不足，不能当成已检验的 alpha
- 收益高度集中于少数个股（前 5 名 86%），换一批成分或错开一个月，结果可能大幅不同
- 成分用的是 2026-11-03 的静态成分回看 2024 年，存在幸存者/前视偏差；趋势股是概念板块，成分本身随行情换手
- vsumn60 是成交量结构因子，本质是"缩量卖压衰竭"而非价格动量，在低流动性小票上容易失真
- 研究/回测，非实盘建议
"""
    (RUN_DIR / f"REPORT_F0_trendstock_vsumn60_cord30_ksft.md").write_text(report, encoding="utf-8")

    # --- stage2 summary: append robustness ---------------------------------
    summary = STAGE2 / "stage2_summary.md"
    body = summary.read_text(encoding="utf-8")
    body = body.replace(
        "## 结论",
        "## 稳健性（分段样本 + 归因）\n\n" + robust_md + "\n\n"
        "- 前 5 只贡献毛收益 86.26%，正贡献标的 39/80 —— 集中度高，非广谱 alpha\n\n"
        "## 结论",
    )
    summary.write_text(body, encoding="utf-8")

    # --- prop file ---------------------------------------------------------
    prop = f"""## 提示词

```
请用 Qlib 158（qlib158）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为趋势股板块（东财概念「趋势股」BK1715，市场扫描 2026-11-03 排名第 2；排除北交所；成分 80 只）上的最优复现：
F0' = 排序 vsumn60 + 硬确认 cord30/ksft。

两段式搜索（结论与"参考金属铜/存储芯片"的关系）：
- 先用金属铜 `kmid2+max60`、存储芯片 `kmid+ksft2` 的确认腿在本池复跑：完整引擎 excess 分别为 -26.50% / -46.79%，直接搬板块组合无效。
- 确认腿在本池重搜后，std30+`cord30+ksft` 的 excess 也只有 -10.35%。
- 真正的增量来自排序腿：把 std30 换成 vsumn60，同确认腿 excess 从 -10.35% 提到 +63.92%（Δexcess=74.27%）。
- 同池 F1（+custom_max_inv 底部否决）Δexcess=-56.27%、Δsharpe=-1.04，未优于 F0'，禁止叠加否决。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
- excess=63.92%>0：本样本跑赢板块等权基准，但 MC p_sharpe=0.5430>0.2，显著性不足。
- 收益集中：前 5 只贡献毛收益 86.26%，正贡献标的 39/80。
- 分段区间 excess 均为正（2024-01~2025-06 +35.99%，2025-07~2026-11 +10.94%），但两段 MC p 分别 0.88 / 0.72。
- 成分固定为 2026-11-03 快照回看 2024 年，存在幸存者/前视偏差；趋势股成分随行情换手。
- 确认腿与排序腿均为本板块样本内重搜结果，过拟合风险高于跨板块通用组合。

【股票池与区间】
- 标的池：A股概念「趋势股」BK1715 成分 80 只（排除北交所）
- 研究/回测参考区间：2024-01-01 至 2026-11-03
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 3 个）】
| ID | 角色 |
|----|------|
| qlib158_vsumn60 | 排序（60 日成交量下跌强度，卖压衰竭；本池 IR 0.178） |
| qlib158_cord30 | 确认（30 日收盘-开盘相关性；IR 0.128） |
| qlib158_ksft | 确认（K 线实体偏移；IR 0.132） |

复制用：qlib158_vsumn60, qlib158_cord30, qlib158_ksft
【禁止】qlib158 反转因子（cntn20/30/60、cntd60）；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑（反向因子在本池先翻转再用）
2. 排序分 S = smooth(qlib158_vsumn60)
3. 确认分 C = IR 加权(cord30:0.128, ksft:0.132)；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 面板只建 OHLCV

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
- 因子 ID + 角色；未使用反转/吸筹/custom；确认腿为本板块存活因子重搜结果
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照：同池纯动量 std30、std30+同确认腿、金属铜/存储芯片参考组合，给出 Δtotal / Δexcess / Δsharpe
- 3–5 条白话：概念板块成分换手与前视偏差、收益集中度、缩量因子在小票上的失真、确认腿样本内过拟合风险
- 声明：研究/回测，非实盘建议

```

## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **F0** / `vsumn60 + cord30/ksft` |
| run | `{RUN_ID}` |
| excess / sharpe / max_dd | {_pct(m.get('excess_return'))} / {_num(m.get('sharpe'))} / {_pct(m.get('max_drawdown'))} |
| total / benchmark | {_pct(m.get('total_return'))} / {_pct(m.get('benchmark_return'))} |
| MC p_sharpe | {_num(mc)} |
| Δexcess vs 纯动量 std30 | +86.87% |
| Δexcess vs std30+cord30/ksft | +74.27% |
| 最近持仓日 | {hold_stats['last_date']}（n={hold_stats['n_held']}） |

{hold_md}

### 本池有效因子 Top10（BK1715 截面 IC/IR）

| # | factor | IC mean | IR | IC>0 占比 |
|--:|--------|--------:|----:|----------:|
{ic_md}

### 引擎排名 Top10（34 组，含动量腿消融）

| # | mom | confirm | excess | total | sharpe | max_dd |
|--:|-----|---------|-------:|------:|-------:|-------:|
{top_md}

### 稳健性

{robust_md}

研究/回测，非实盘建议。
"""
    PROP.write_text(prop, encoding="utf-8")

    # --- drop the superseded stage-1 publish from the reports listing -------
    if OLD_RUN.is_dir():
        SUPERSEDED.mkdir(parents=True, exist_ok=True)
        target = SUPERSEDED / OLD_RUN.name
        if target.exists():
            target = SUPERSEDED / f"{OLD_RUN.name}_2"
        shutil.move(str(OLD_RUN), str(target))
        print(f"[moved superseded] {target}")

    print(f"wrote {PROP}")
    print(f"updated {summary}")
    print(f"wrote {RUN_DIR / 'REPORT_F0_trendstock_vsumn60_cord30_ksft.md'}")


if __name__ == "__main__":
    raise SystemExit(main())
