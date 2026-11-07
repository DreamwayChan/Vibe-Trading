# Capability circle

English. 中文: [CAPABILITY_zh.md](CAPABILITY_zh.md).

This fork is a **day-to-swing, long-only** research and paper-trading tool for **mid and small capital** on the A-share cash market. Rules stay explainable: a person can read the hypothesis, the invalidation, the planned position, and the planned risk. The model does not sit on the order path.

## Default path

```text
lake → hypothesis → cost-aware backtest → validation gate → tradable → paper_book
```

| Step | What it is | Where |
|---|---|---|
| Lake | Settled daily bars. Beijing "today" is not rewritten as a finished bar. | `agent/src/data/lake.py` |
| Hypothesis | A journal row with a thesis (or hypothesis id), invalidation, planned position, planned risk, and a timestamp. | `vibe-trading hypothesis`, `agent/src/hypotheses/journal.py` |
| Cost-aware backtest | Fills use A-share commission, stamp, transfer, slippage, T+1, and limit/halt rules. | `agent/backtest/ashare_costs.py` |
| Validation gate | Monte Carlo, bootstrap, and walk-forward must clear their thresholds on that cost-aware run. | `agent/backtest/validation.py` |
| Tradable | The monitor pool opens only when the journal and the gate both pass. Buys on ST, limit-up, one-word boards, or a 3-board streak stay out. | `agent/src/monitor/admission.py` |
| Paper book | One session, or a calendar range of consecutive sessions. Daily report plus a window report: NAV, fills, unfilled, turnover, and cumulative deviation versus the bound backtest. A deviation past the threshold still clears `tradable`. Single-name cap and a drawdown cap on new buys. | `vibe-trading paper-book` or `python -m src.paper_book` |

```bash
vibe-trading hypothesis list
vibe-trading paper-book step --date YYYY-MM-DD --run-id <id>
vibe-trading paper-book soak --days 20 --end YYYY-MM-DD --run-id <id>
python -m src.paper_book soak --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>
python -m src.paper_book show --book-id <id> --window
```

`POST /paper-book/step` settles one session. `POST /paper-book/soak` settles the range (`{"end": "YYYY-MM-DD", "days": 20}` or `{"start", "end"}`).

## Cost tax, side by side

The default backtest stays cost-aware. To see the same strategy, bars, and window with no fees and no limit or halt blocks, run the named `frictionless` profile next to `ashare_costs`:

```bash
python -m backtest.cost_compare <run_dir>
vibe-trading cost-compare <run_dir>
```

`POST /runs/{run_id}/cost-compare` returns the same JSON and writes `artifacts/cost_compare.json`. `GET /runs/{run_id}` includes that object as `cost_compare` when the file is present. The comparison does not replace `metrics.csv` or the run card. Monitor re-bench stays on the single cost-aware engine.

`metrics.delta` is **ashare_costs − frictionless** (the cost tax). A negative `total_return` or `nav` means fees, slippage, or refused limit/halt/T+1 orders reduced the result. `nav` is ending equity (`final_value`). `fill_count` includes opening fills, signal exits, and the terminal liquidation.

Frictionless knobs are fixed by `FRICTIONLESS_PROFILE` in `agent/backtest/ashare_costs.py`. They replace `config.json` and `VIBE_TRADING_ASHARE_*`.

| Knob | Frictionless | Cost-aware default |
|---|---|---|
| `commission_rate` | 0 | 0.00025 (万2.5) |
| `commission_min` | 0 | ¥5 |
| `stamp_tax` | 0 | 0.0005 (万5, sell only) |
| `transfer_fee` | 0 | 0.00001 (万0.1) |
| `slippage` | 0 | 0.001 |
| `t_plus_one` | false (same-day sells allowed) | true |
| `block_suspension` | false (halts and zero-volume bars may fill) | true |
| `enforce_price_limits` | false (limit-up buys and limit-down sells may fill) | true |

Short sales stay blocked. Buys still round down to 100-share lots. The tradability panel may still be attached so both legs share it; with `block_suspension` and `enforce_price_limits` false, frictionless does not apply halt flags or price bands. There is no environment switch for `enforce_price_limits`. Omitting it keeps the cost-aware default.

Sessions come from the A-share calendar (`agent/src/data/calendar.py`). Weekends and exchange holidays are not sessions. With no calendar seed, weekdays are used. Each session trades that day's monitor snapshot when `~/.vibe-trading/monitor/history/<run_id>/<date>.json` exists, otherwise the live board when its suggestions are dated that session. Days with neither still mark the book and write a daily report. `--signals schedule.json` replays an explicit schedule, either `{"YYYY-MM-DD": [{"symbol", "action", "weight"}]}` or a list of rows with `as_of`. `tradable` defaults to true in that file. The window report (NAV, fills, unfilled, turnover, cumulative deviation versus the bound run's `artifacts/equity.csv`) is `reports/window.json` under the book root. A deviation past `VIBE_TRADING_PAPER_BOOK_DEVIATION_THRESHOLD` still clears `tradable` for the rest of the window. Nothing on this path sends a broker order.

## What this is for

- Holding period: days to a few weeks, not seconds.
- Direction: long only. Exits are sells of shares the book already holds (T+1).
- Size: mid and small capital. Default paper cash is `1_000_000`. One name defaults to **20%** of marked NAV (`VIBE_TRADING_PAPER_BOOK_MAX_POSITION`). New buys stop after a **15%** peak-to-mark drawdown (`VIBE_TRADING_PAPER_BOOK_MAX_DRAWDOWN`). Sells still settle.
- A-share markers, when the bar or the tradability panel can support them:
  - **limit-up** and **one-word board** (open at the ceiling, low never left it)
  - **ST** (panel `is_st`, a bar flag, or a name starting with `ST` / `*ST`)
  - **consecutive board** (2 limit-up sessions) and **extreme sentiment** (3 or more on this name)

Extreme sentiment here is the name's own streak. A market-wide emotion index is not on the bar panel, so it is not invented.

## Explicit bans

- No full-auto get-rich narrative. A passing gate is permission to **paper-trade**, not a promise of profit.
- No race against institutional HFT or exchange-colocation alpha. The clock is the daily bar.
- No LLM on the auto-order path. The model may help write a hypothesis. It does not approve `tradable`, size the paper book, or place an order. Broker orders stay on the upstream mandate layer, outside this loop.

## Research-only (frozen)

These surfaces still load. They are not the default, and they are not evidence a signal is tradable.

- `agent/prop_*.md` — frozen sector and factor prompts
- `agent/scripts/run_f0_f1_*.py`, `agent/scripts/run_sectors_optimal_*.py` — the batch jobs that wrote those prompts
- `agent/src/factors/zoo/custom/` — seven unverified theme factors
- Sector Pick's 「最优」 catalog (`agent/src/sector_pick/catalog.py`) — recipes that point at those prompts

Market Pick, Sector Pick, and the analog forecast remain available as research boards. A name becomes tradable only through the path above.
