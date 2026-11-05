# prop · GTJA191 存活因子 趋势股 BK1715 最优回测复现

> 研究/回测，非实盘建议。

## 一句话

在 趋势股 BK1715（80 只成分，2024-01-01~2026-11-03）上，用**国泰君安 GTJA191 存活因子**（survival store alive 8 个）重做 F0' 流程，
最优是 动量腿 `124` + 确认腿 `002+168`：
excess **58.80%**、total **99.12%**、sharpe **1.8563**、max_dd **-11.13%**。

## 复现步骤

```bash
cd agent
python scripts/research_gtja_trendstock_confirm_search.py --top-k 12 --mc-sims 1000
```

## 关键参数

- 因子：`gtja191_002, gtja191_168`（确认腿，存活 IR 加权）+ `gtja191_124`（动量腿）
- 组合：N=6 等权，保留带 Top9，换手帽 20%，最小持有 5 日，峰值回撤 15% 止损，止盈 35%，MA20 择时
- 确认门槛：加权分位 ≥ 40%

## 交付

- 回测记录：`runs/20261103_232628_05_F0_trendstock_gtja_124__002_168`
- 搜索明细：`runs/_research_gtja_trendstock_confirm_search/`

## 稳健性（务必一起看）

| 检查 | 结果 |
|---|---|
| 分段 excess 2024/2025/2026YTD | +36.77% / +5.05% / -5.24% |
| 确认腿阶梯 | pure +21.80% → 002 +15.33% → 168 +32.23% → 002+168 +58.80% |
| 确认腿跨动量腿边际（002+168） | 平均 +0.60%，正增益 2/7 |
| 最优可迁移确认腿 | `124+002`：平均 +25.59%，最差 +15.07%，7/7 |
| 收益集中度 | 前 5 只 81.36%，正贡献 29/80 |
| MC p_sharpe | 0.253（>0.2，不显著） |

## 文件

- run 记录：`runs/20261103_232628_05_F0_trendstock_gtja_124__002_168`
- 搜索与稳健性：`runs/_research_gtja_trendstock_confirm_search/gtja_summary.md`
- 脚本：`scripts/research_gtja_trendstock_confirm_search.py` / `analyze_gtja_winner.py` / `ablate_gtja_confirm.py` / `delta_gtja_confirm.py` / `finalize_gtja_report.py`

