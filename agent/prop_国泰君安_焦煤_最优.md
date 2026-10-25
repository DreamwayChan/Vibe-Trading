## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行。

【背景与本版目标】
本版为焦煤板块（东财行业「焦煤」BK1494（排除北交所；市值优先最多80只））慢牛过程优化：
确认腿沿用 F0' 重搜结果 093/171；过程改为 `no_timing`（相对周频/MA20/紧止盈止损的基线）。
优化看 total_return 与 sharpe；对照基线 Δtotal=4.96%，Δsharpe=0.0770。

【必须承认（本样本）】
- total=9.94%，excess=25.96%，sharpe=0.2742
- MC p_sharpe=0.5040
- 过程参数样本内重搜有过拟合风险；焦煤价格周期/钢厂需求扰动

【股票池与区间】
- 标的池：A股焦煤（东财行业「焦煤」BK1494（排除北交所；市值优先最多80只））
- 区间：2024-01-01 至 2026-08-26
- 因子：gtja191_164, gtja191_093, gtja191_171
- 过程：{"name": "no_timing", "ma_period": 20, "timing_off": true, "peak_dd_sl": 0.15, "tp": 0.35, "min_hold_days": 5, "time_stop_days": 20, "rank_worsen": 10, "top_n": 6, "band": 9, "max_turnover": 0.2, "confirm_q": 0.4, "rebalance": "weekly"}

【交付】
- 前端 run：`20260827_090400_05_F0_coking_slowbull_no_timing`
- 对照基线：`20260827_083500_03_F0_coking_093_171`
- 声明：研究/回测，非实盘建议
```

## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **慢牛过程 `no_timing`** / 确认 `093+171` |
| 优化 run | `20260827_090400_05_F0_coking_slowbull_no_timing` |
| 基线 F0' | `20260827_083500_03_F0_coking_093_171` |
| total / excess / sharpe / max_dd | 9.94% / 25.96% / 0.2742 / -32.36% |
| Δtotal / Δsharpe vs 基线 | 4.96% / 0.0770 |
| MC p_sharpe | 0.5040 |
| 最近持仓日 | 2026-08-26（n=6） |

**声明：研究/回测结果，非实盘建议。**
