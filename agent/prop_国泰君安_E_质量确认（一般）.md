## 提示词

```
请用国泰君安 191（gtja191）+ custom 质量确认因子做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
相对已验证的 C 结构（gtja191_164 主排序 + 114/171/054 确认 + 周频/换手帽/最小持有），
以及效果不佳的 D 吸筹版（custom_comp_low / custom_near_low 偏「低位×压缩」反转逻辑，与动量主排序冲突），
本版 E 只做一件事：用学术稳健的「质量/流量」custom 因子替换吸筹确认腿，不改过程约束。

金融依据（OHLCV 可算、A股文献常见）：
1. custom_ivol_inv：−特质波动（Ang et al. 2006）——低噪音质量过滤
2. custom_abn_to_inv：−异常换手 5d/60d（Datar；A股低换手溢价）——去拥挤
3. custom_max_inv：−近月最大单日涨幅（Bali et al. 2011 MAX）——反彩票
4. custom_vp_corr：收益–成交量滚动相关——量价配合确认趋势
5. （可选主排序对照）custom_risk_mom：20d 均收益/波动，与 164 构造不同

本版过程约束与 C 相同；优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。
若相对 C 无增量，明文写「质量确认未带来增量」，禁止为追收益加回吸筹/反转。

【股票池与区间】
- 标的池：A股人形机器人板块
- 研究/回测参考区间：2024-01-01 至 2026-07-24（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版仅 5 个）】
| ID | 角色 |
| gtja191_164 | 动量（主排序，必须） |
| custom_ivol_inv | 质量确认（低特质波动） |
| custom_abn_to_inv | 质量确认（低异常换手） |
| custom_max_inv | 质量确认（反彩票 MAX） |
| custom_vp_corr | 量价确认 |

复制用：gtja191_164, custom_ivol_inv, custom_abn_to_inv, custom_max_inv, custom_vp_corr
【禁止】gtja191_120 及一切反转因子；禁止 081/111/163/017/012/002；
禁止 custom_comp_low / custom_near_low（本版明确不用吸筹）；
禁止 gtja191_054/114/171（本版由 custom 质量确认替代其确认席位）；
禁止库内其他因子；禁止把同花顺公式原文粘进 signal_engine

【评分（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量，不混质量分）
3. 确认分 C = 等权(custom_ivol_inv, custom_abn_to_inv, custom_max_inv, custom_vp_corr)；缺谁跳谁并在剩余项上重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 已持仓续持看 S 的保留带，不强制再过确认过滤

【持仓与换手（硬性）】
1. N=6 等权；保留带 Top9（N×1.5）
2. 调仓日：确认过滤后按 S 选 Top6；旧仓仍在 Top9 且未触发风控可续持
3. 换手硬顶 20%：优先留旧仓，再填新股；将超 20% 则停止换入（空仓建仓首期除外）
4. 最小持有 5 日：未满 5 日不因跌出保留带而卖；峰值回撤止损/止盈立即生效
5. 择时：板块等权均价 vs MA20，仅调仓日更新乘数（1.0/0.5）；非调仓日冻结

【退出】
峰值回撤≥15% → 止盈≥35% → 持仓≥20日且排名恶化≥10名 → 调仓日跌出 Top9（且满 5 日）
非调仓日不因排名换股。

【交付】
- 因子 ID + 角色；说明质量确认来自 IVOL/换手/MAX/量价，不是 Level-2 主力资金；未使用反转/吸筹
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p（若有）
- 相对 C 结构：若 excess 未提升，明文写「质量确认未带来增量」
- 3–5 条白话：过程约束、相对基准、主要风险
- 声明：研究/回测，非实盘建议

```
