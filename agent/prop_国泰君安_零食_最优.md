## 提示词

```
请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为零食板块（东财行业「零食」BK1583）上的最优复现：F1 = F0 硬确认 + custom_max_inv 底部否决。
同池实测（run `20260729_150000_02_F1_snack` vs `20260729_150000_01_F0_snack`）：
- F1 相对 F0：Δtotal=25.71%，Δexcess=25.71%，Δsharpe=0.4645
- 结构依据仍来自 BK1184 bake-off（veto_max_20）；本版不重做因子挖掘。

本版只做一件事：在零食上复用 F0 + custom_max_inv 底部否决，不改过程约束。
优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认；若不及同池 F0 明文写「F1 未在零食带来增量」；若明显弱于板块基准明文写「F1 结构未在零食迁移成功」，禁止为追收益擅自加反转/扩因子。

【股票池与区间】
- 标的池：A股零食相关板块（东财行业「零食」BK1583，排除北交所）
- 研究/回测参考区间：2024-01-01 至 2026-07-28（数据不足则用可得区间并说明）
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
- 3–5 条白话：为何否决不同于 E 硬过滤、相对基准、过拟合风险（含渠道/原料成本/消费景气周期）
- 声明：研究/回测，非实盘建议

```
