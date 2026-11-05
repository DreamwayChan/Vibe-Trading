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
| 假设 | 一条日志：论点（或假设 id）、失效条件、计划仓位、计划风险、时间戳。 | `vibe-trading hypothesis`，`agent/src/hypotheses/journal.py` |
| 计成本回测 | 成交使用 A 股佣金、印花税、过户费、滑点、T+1、涨跌停与停牌规则。 | `agent/backtest/ashare_costs.py` |
| 验证门 | 蒙特卡洛、bootstrap、走步检验都要过线，而且回测必须计成本。 | `agent/backtest/validation.py` |
| 可交易 | 监控池只在日志和验证门都通过时打开。ST、涨停、一字板、三连板以上的买入仍然排除。 | `agent/src/monitor/admission.py` |
| 纸面账本 | 用这些可交易信号逐日结算。成交规则相同。单名仓位上限，回撤过大则停止新开仓。 | `vibe-trading paper-book` 或 `python -m src.paper_book` |

```bash
vibe-trading hypothesis list
vibe-trading paper-book step --date YYYY-MM-DD --run-id <id>
python -m src.paper_book show --book-id <id>
```

`POST /paper-book/step` 是同一个日结。这条路径不会向券商发单。

## 做什么

- 持有期：数日到数周，不是秒级。
- 方向：只做多。卖出只卖掉账本里已经有的股份（T+1）。
- 规模：中小资金。纸面初始资金默认 `1000000`。单只股票默认不超过盯市净值的 **20%**（`VIBE_TRADING_PAPER_BOOK_MAX_POSITION`）。自高点回撤达到 **15%** 后停止新买入（`VIBE_TRADING_PAPER_BOOK_MAX_DRAWDOWN`）。卖出照常结算。
- A 股风险标记（K 线或可交易性面板能支撑时才标）：
  - **涨停** 与 **一字板**（开在涨停价，最低价没有离开涨停价）
  - **ST**（面板 `is_st`、K 线上的标记，或名称以 `ST` / `*ST` 开头）
  - **连板**（连续 2 个涨停）与 **情绪极值**（这只股票自身连续 3 个及以上涨停）

这里的情绪极值是这只股票自己的连板，不是全市场情绪指数。K 线面板上没有那个序列，就不编造。

## 明确禁止

- 禁止全自动致富叙事。验证门通过只表示可以 **纸面交易**，不是收益承诺。
- 禁止和机构高频、交易所托管的速度赛跑。时钟是日线。
- 禁止把 LLM 放在自动下单主路径上。模型可以帮忙写假设。它不批准 `tradable`，不决定纸面仓位，也不下单。券商下单仍在这条闭环之外的上游 mandate 层。

## 仅研究（冻结）

下面这些还能打开。它们不是默认路径，也不能当成「信号已经可交易」的证据。

- `agent/prop_*.md` — 冻结的板块与因子提示词
- `agent/scripts/run_f0_f1_*.py`、`agent/scripts/run_sectors_optimal_*.py` — 写出这些提示词的批处理
- `agent/src/factors/zoo/custom/` — 七个未经本闭环验证的主题因子
- 优选板块的「最优」目录（`agent/src/sector_pick/catalog.py`）— 指向这些提示词的配方

市场优选、优选板块和相似片段推演仍可作为研究看板。一只股票要变成可交易，只能走上面那条路径。
