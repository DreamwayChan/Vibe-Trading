## 提示词

```
请用 Qlib 158（qlib158）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为金属铜板块（东财行业「铜」BK1615）上的最优复现：
F0' = 动量 std30 + 硬确认 kmid2/max60（Qlib158 存活因子确认腿重搜结果）。
对照纯动量 std30：Δexcess=29.84%。
同池实测 F1'（kmid2/max60 + MAX 底部否决）未带来可采纳增量（Δexcess=0.45%，Δsharpe=-0.0341），故默认回退 F0'。
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
| qlib158_std30 | 动量（主排序，必须） |
| qlib158_kmid2 | 确认（F0' 硬确认） |
| qlib158_max60 | 确认（F0' 硬确认） |

复制用：qlib158_std30, qlib158_kmid2, qlib158_max60
【禁止】qlib158 反转因子（cntn20/30/60、cntd60）；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排
【禁止】回退使用 IR 朴素对 cord60/klen 作为本版确认腿（快筛/完整引擎均劣于 std30 纯动量）

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(qlib158_std30)（纯动量）
3. 确认分 C = IR 加权(kmid2:0.095, max60:0.117)；缺谁跳谁并重归一
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
- 因子 ID + 角色；未使用反转/吸筹/custom；确认腿为 kmid2/max60 非 cord60/klen
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照：同池 F1' 与纯动量 std30，给出 Δtotal / Δexcess / Δsharpe；说明为何以 F0' 为准
- 3–5 条白话：相对基准、铜价周期/确认腿样本内过拟合风险
- 声明：研究/回测，非实盘建议

```
