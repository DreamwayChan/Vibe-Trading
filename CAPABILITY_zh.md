# 能力圈

中文。English: [CAPABILITY.md](CAPABILITY.md).

本分支是面向 **中小资金**、**只做多**、**日线到波段** 的 A 股现金交易研究与纸面工具。规则要能讲清楚：假设、失效条件、计划仓位、计划风险都写在日志里。模型不站在下单路径上。

## 默认路径

```text
数据湖 → 假设 → 计成本回测 → 验证门 → 可交易 → 纸面账本
```

| 步骤 | 是什么 | 在哪 |
|---|---|---|
| 数据湖 | 已结算日线。北京时间的「今天」不会被写成一根完成的 K 线。 | `agent/src/data/lake.py` |
| 假设 | 可交易之前要有五栏：客观观察、因果假设（因为 / 因此）、逻辑、验证计划（数据、窗口、指标、成本）、失效条件。计划仓位、计划风险和时间戳仍然必填。缺任何一栏就只留在研究板上。严格模式返回 422。异象（涨停次日回吐）和行为偏差（周末风险厌恶）的入口模板会预填这五栏。它们只写出机制，不报告已测出的优势。 | `vibe-trading hypothesis`，`agent/src/hypotheses/journal.py`，`agent/src/hypotheses/templates.py` |
| 计成本回测 | 成交使用 A 股佣金、印花税、过户费、滑点、T+1、ST 禁买、涨跌停与停牌规则。 | `agent/backtest/ashare_costs.py` |
| 验证门 | 蒙特卡洛、bootstrap、走步检验都要过线，而且回测必须计成本。这次通过不审阅样本外。 | `agent/backtest/validation.py` |
| 可交易 | 监控池只在五栏假设和验证门都通过时打开。样本外开关关闭时，验证门通过表示样本外未审阅。最新收盘价穿过价格失效线时，该标的被清出。ST、涨停、一字板、三连板以上的买入仍然排除。没有可交易日志的持仓会列成提醒。「最优」目录里的回测即使日志和验证门都过，也以 `uncausal_scan` 留在池外；例外是它同时带着决策记录（验证门通过 + 日志指向注册表里未被否决的假设）——货架是从存活报告重建的，会把看板绑定的 run 一起收进去，单凭上货架不撤销入池资格。 | `agent/src/monitor/admission.py` |
| 纸面账本 | 单日，或按交易日历连续多日。日报加窗口报表：净值、成交、未成交、换手，以及相对绑定回测的累计偏离。偏离超过阈值、价格失效，或边缘衰减超过阈值，都会取消可交易并降级账本。衰减清除默认打开（`VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE=0` 时衰减只作警告）。单名仓位上限，回撤过大则停止新开仓。已复盘的日志保留实现盈亏和胜率。 | `vibe-trading paper-book` 或 `python -m src.paper_book` |

```bash
vibe-trading hypothesis list
vibe-trading paper-book step --date YYYY-MM-DD --run-id <id>
vibe-trading paper-book soak --days 20 --end YYYY-MM-DD --run-id <id>
python -m src.paper_book soak --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>
python -m src.paper_book show --book-id <id> --window
```

`POST /paper-book/step` 结算一个交易日。`POST /paper-book/soak` 结算一段区间（`{"end": "YYYY-MM-DD", "days": 20}` 或 `{"start", "end"}`）。

监控看板用 `GET /paper-book/{book_id}?window=true` 读取绑定回测的最新窗口（账本 id 就是 run id）。面板显示净值、浮动盈亏（盯市减去已保存的批次成本）、累计偏离、边缘衰减，以及相对 20 个交易日的浸泡进度，并标出这块板是否还在可交易池里。偏离和衰减不在面板里重算。

### 样本外政策

验证门通过看的是蒙特卡洛、bootstrap、走步检验和成本。这次通过不审阅样本外。

样本外未审阅。只有 `VIBE_TRADING_VAL_REQUIRE_OUT_OF_SAMPLE=1` 时才做这项检查。

开关打开后，验证门读取 `validation.json` 的 `out_of_sample`。这一块是同一条权益曲线的尾部切分（配置里写了 `config["validation"]["out_of_sample"]` 时，默认取最后 `oos_fraction` 0.3）。它不是独立的市场留出样本。缺这一块、`degradation` 高于 `VIBE_TRADING_VAL_MAX_OOS_DEGRADATION`（默认 0.5）、或尾部夏普不为正，验证门不通过。开关关闭时，验证门通过仍然表示样本外未审阅。

## 成本税，并排

默认回测仍然计成本。同一策略、同一批 K 线、同一窗口，要看「无成本」和「成本+涨跌停」并排结果，用命名的 `frictionless` 配置对照 `ashare_costs`：

```bash
python -m backtest.cost_compare <run_dir>
vibe-trading cost-compare <run_dir>
```

`POST /runs/{run_id}/cost-compare` 返回同一份 JSON，并写入 `artifacts/cost_compare.json`。文件存在时，`GET /runs/{run_id}` 的 `cost_compare` 字段带上它。这次对照不会替换 `metrics.csv` 或 run card。监控里的重新回测仍然只跑计成本的那一条引擎。

监控看板会给当前绑定的回测显示无成本、计成本和差额。收益差额为负时，一行写明「成本吃掉 X%」。`GET /runs/{run_id}/cost-compare` 读取这份文件。面板上的「计算成本对照」调用同一个 `POST`。面板自己不算成本税。

`metrics.delta` 是 **成本+涨跌停 − 无成本**（成本税）。`total_return` 或 `nav` 为负，表示佣金、滑点，或涨跌停、停牌、T+1 拒单拉低了结果。`nav` 是期末权益（`final_value`）。`fill_count` 含开仓、信号平仓，以及回测结束时的强制平仓。

无成本旋钮写死在 `agent/backtest/ashare_costs.py` 的 `FRICTIONLESS_PROFILE`。它们覆盖 `config.json` 和 `VIBE_TRADING_ASHARE_*`。

| 旋钮 | 无成本 | 计成本默认 |
|---|---|---|
| `commission_rate` | 0 | 0.00025（万 2.5） |
| `commission_min` | 0 | 5 元 |
| `stamp_tax` | 0 | 0.0005（万 5，仅卖出） |
| `transfer_fee` | 0 | 0.00001（万 0.1） |
| `slippage` | 0 | 0.001 |
| `t_plus_one` | false（允许当日卖出） | true |
| `block_suspension` | false（停牌和零成交可以成交） | true |
| `block_st_buy` | false（允许买入 ST/*ST） | true |
| `enforce_price_limits` | false（涨停可以买、跌停可以卖） | true |

融券卖空仍然禁止。买入仍然向下取整到 100 股。可交易性面板可以仍然挂在两次运行上，两边看到的是同一个对象；`block_suspension` 和 `enforce_price_limits` 为 false 时，无成本路径不使用停牌标记和涨跌停价。`enforce_price_limits` 没有环境变量。不写这个键就保持计成本默认。

交易日来自 A 股日历（`agent/src/data/calendar.py`）。周末和交易所休市日不是交易日。没有日历种子时按工作日计算。每个交易日优先用当天的监控快照 `~/.vibe-trading/monitor/history/<run_id>/<date>.json`；没有快照时，如果当前监控板的建议日期就是这一天，就用这块板。两样都没有的日子仍然盯市并写日报。`--signals schedule.json` 用显式日程回放，格式是 `{"YYYY-MM-DD": [{"symbol", "action", "weight"}]}`，或带 `as_of` 的列表。文件里省略 `tradable` 时视为可交易。「最优」目录里的 run id 即使把 `tradable` 写成 true，仍以 `uncausal_scan` 拒绝，除非该板带着决策记录（验证门通过 + 日志指向未被否决的假设）。窗口报表（净值、成交、未成交、换手、相对绑定回测 `artifacts/equity.csv` 的累计偏离）写在账本目录的 `reports/window.json`。累计偏离超过 `VIBE_TRADING_PAPER_BOOK_DEVIATION_THRESHOLD` 后，`tradable` 被取消，窗口里之后的交易日不再成交。窗口至少 20 个交易日时，边缘衰减超过 `VIBE_TRADING_PAPER_BOOK_MAX_DECAY`（默认 0.5）也会取消可交易，除非 `VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE` 关掉。日志可以用 `invalidation_kind` 写更紧的偏离或衰减阈值。这条路径不会向券商发单。

## 做什么

- 持有期：数日到数周，不是秒级。
- 方向：只做多。卖出只卖掉账本里已经有的股份（T+1）。
- 规模：中小资金。纸面初始资金默认 `1000000`。单只股票默认不超过盯市净值的 **20%**（`VIBE_TRADING_PAPER_BOOK_MAX_POSITION`）。自高点回撤达到 **15%** 后停止新买入（`VIBE_TRADING_PAPER_BOOK_MAX_DRAWDOWN`）。可选买入流动性上限（`VIBE_TRADING_PAPER_BOOK_MAX_VOLUME_RATIO`）会把单只股票当日买入股数限制在该 K 线成交量的一定比例（0 表示关闭）。卖出照常结算。
- A 股风险标记（K 线或可交易性面板能支撑时才标）：
  - **涨停** 与 **一字板**（开在涨停价，最低价没有离开涨停价）
  - **ST**（面板 `is_st`、K 线上的标记，或名称以 `ST` / `*ST` 开头）
  - **连板**（连续 2 个涨停）与 **情绪极值**（这只股票自身连续 3 个及以上涨停）

这里的情绪极值是这只股票自己的连板，不是全市场情绪指数。K 线面板上没有那个序列，就不编造。

## 明确禁止

- 禁止全自动致富叙事。验证门通过只表示可以 **纸面交易**，不是收益承诺。这次通过不审阅样本外。除非 `VIBE_TRADING_VAL_REQUIRE_OUT_OF_SAMPLE=1`，否则样本外未审阅。
- 禁止和机构高频、交易所托管的速度赛跑。时钟是日线。
- 禁止把 LLM 放在自动下单主路径上。模型可以帮忙写假设。它不批准 `tradable`，不决定纸面仓位，也不下单。券商下单仍在这条闭环之外的上游 mandate 层。
- 新功能必须挂上这条闭环，否则拒绝。没有因果假设的扫描、因子卡片、「最优」配方或回放只保留为研究，不能把 `tradable` 设为真。

## 合入门禁

代码合入的条件是离线 CI pytest 全绿。命令与 `.github/workflows/test.yml` 相同，写在 `CONTRIBUTING.md`。真实端到端测试不在这道门里。

## 仅研究（冻结）

下面这些还能打开。它们不是默认路径，也不能当成「信号已经可交易」的证据。

- `agent/prop_*.md` — 冻结的板块与因子提示词
- `agent/scripts/run_f0_f1_*.py`、`agent/scripts/run_sectors_optimal_*.py` — 写出这些提示词的批处理
- `agent/src/factors/zoo/custom/` — 七个未经本闭环验证的主题因子
- 优选板块的「最优」目录（`agent/src/sector_pick/catalog.py`）— 指向这些提示词的配方。每一行都是 `research_only`。优选板块和市场优选会标「仅研究」，避免看起来像可交易路径。把这些 run id 绑到监控上也不会准入：准入返回 `uncausal_scan`，`tradable` 保持为假。唯一例外是该 run 同时带着决策记录（验证门通过 + 日志指向未被否决的假设），此时货架行只是存活报告的展示行，看板继续可交易。因子卡片和风向扫描带 `tradable: false`。纸面日程不能为没有该记录的配方成交。

市场优选、优选板块和相似片段推演仍可作为研究看板。监控看板上，还没通过日志和验证门的行放在虚线研究板里；可交易信号单独一栏。一只股票要变成可交易，只能走上面那条路径。填入异象或行为偏差模板也不会把「最优」回测推进池子。
