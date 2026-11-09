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
| Hypothesis | Five fields before a name is tradable: an objective observation, a causal hypothesis (because / therefore), the logic, a validation plan (data, window, metrics, costs), and an invalidation. Planned position, planned risk, and a timestamp stay required. A missing field stays on the research board. Strict mode returns 422. | `vibe-trading hypothesis`, `agent/src/hypotheses/journal.py` |
| Cost-aware backtest | Fills use A-share commission, stamp, transfer, slippage, T+1, ST buy blocks, and limit/halt rules. | `agent/backtest/ashare_costs.py` |
| Validation gate | Monte Carlo, bootstrap, and walk-forward must clear their thresholds on that cost-aware run. That pass does not review out-of-sample. | `agent/backtest/validation.py` |
| Tradable | The monitor pool opens only when the five hypothesis fields and the gate both pass. A gate pass with out-of-sample left off means OOS not reviewed. A price invalidation that the latest close has crossed clears that name. Buys on ST, limit-up, one-word boards, or a 3-board streak stay out. Holdings with no tradable journal entry are listed as alerts. | `agent/src/monitor/admission.py` |
| Paper book | One session, or a calendar range of consecutive sessions. Daily report plus a window report: NAV, fills, unfilled, turnover, and cumulative deviation versus the bound backtest. A deviation past the threshold, a price invalidation, or edge decay past the threshold clears `tradable` and downgrades the book. Decay clearing is on by default (`VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE=0` leaves decay as a warning). Single-name cap and a drawdown cap on new buys. Reviewed journal rows keep a realised PnL and a win rate. | `vibe-trading paper-book` or `python -m src.paper_book` |

```bash
vibe-trading hypothesis list
vibe-trading paper-book step --date YYYY-MM-DD --run-id <id>
vibe-trading paper-book soak --days 20 --end YYYY-MM-DD --run-id <id>
python -m src.paper_book soak --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>
python -m src.paper_book show --book-id <id> --window
```

`POST /paper-book/step` settles one session. `POST /paper-book/soak` settles the range (`{"end": "YYYY-MM-DD", "days": 20}` or `{"start", "end"}`).

Monitor reads the latest window for the bound run with `GET /paper-book/{book_id}?window=true` (the book id is the run id). The panel shows NAV, unrealized mark-minus-lot-cost, cumulative deviation, edge decay, and soak progress against 20 sessions, next to whether the board is in the tradable pool. It does not recompute decay or deviation.

### Out-of-sample policy

The validation gate passes on Monte Carlo, bootstrap, walk-forward, and costs. That pass does not review out-of-sample results.

OOS not reviewed. The check runs only when `VIBE_TRADING_VAL_REQUIRE_OUT_OF_SAMPLE=1`.

With the flag on, the gate reads the `out_of_sample` block in `validation.json`. That block is a tail split of the same equity curve (the last `oos_fraction`, default 0.3, when `config["validation"]["out_of_sample"]` is set). It is not an independent market holdout. The gate fails when the block is missing, when `degradation` is above `VIBE_TRADING_VAL_MAX_OOS_DEGRADATION` (default 0.5), or when the tail Sharpe is not positive. With the flag off, a gate pass still means OOS not reviewed.

### Alpha / Beta and edge decay

- Backtest `metrics.csv` and the run card now carry `beta`, annualised `alpha`, and `r_squared`, from an OLS of strategy returns on the benchmark returns (`benchmark` in config, otherwise the equal-weight universe). Read `alpha`, not total return: a high return with `beta` near 1 and `alpha` near 0 is market luck.
- The paper-book window report (`reports/window.json`) has a `decay` block (`src/paper_book/decay.py`). It compares the last 10 sessions with the earlier ones: `recent_sharpe` vs `earlier_sharpe`, `decay` (relative Sharpe drop), and `recent_gap_mean` (paper minus backtest daily return). `decayed` is true when `decay` exceeds `VIBE_TRADING_PAPER_BOOK_MAX_DECAY` (default 0.5), or when returns turn from positive to negative. It needs at least 20 sessions. When `decayed` is true, `tradable` is cleared unless `VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE` is off. A journal row can set a tighter decay or deviation level with `invalidation_kind`.

## Cost tax, side by side

The default backtest stays cost-aware. To see the same strategy, bars, and window with no fees and no limit or halt blocks, run the named `frictionless` profile next to `ashare_costs`:

```bash
python -m backtest.cost_compare <run_dir>
vibe-trading cost-compare <run_dir>
```

`POST /runs/{run_id}/cost-compare` returns the same JSON and writes `artifacts/cost_compare.json`. `GET /runs/{run_id}` includes that object as `cost_compare` when the file is present. The comparison does not replace `metrics.csv` or the run card. Monitor re-bench stays on the single cost-aware engine.

Monitor shows that comparison for the bound run: zero-cost, after-cost, and delta, including the line “costs ate X%” when the return delta is negative. `GET /runs/{run_id}/cost-compare` reads the artifact. The Compare button calls the same `POST`. The panel does not recompute the tax.

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
| `block_st_buy` | false (ST/*ST names may be bought) | true |
| `enforce_price_limits` | false (limit-up buys and limit-down sells may fill) | true |

Short sales stay blocked. Buys still round down to 100-share lots. The tradability panel may still be attached so both legs share it; with `block_suspension` and `enforce_price_limits` false, frictionless does not apply halt flags or price bands. There is no environment switch for `enforce_price_limits`. Omitting it keeps the cost-aware default.

Sessions come from the A-share calendar (`agent/src/data/calendar.py`). Weekends and exchange holidays are not sessions. With no calendar seed, weekdays are used. Each session trades that day's monitor snapshot when `~/.vibe-trading/monitor/history/<run_id>/<date>.json` exists, otherwise the live board when its suggestions are dated that session. Days with neither still mark the book and write a daily report. `--signals schedule.json` replays an explicit schedule, either `{"YYYY-MM-DD": [{"symbol", "action", "weight"}]}` or a list of rows with `as_of`. `tradable` defaults to true in that file. The window report (NAV, fills, unfilled, turnover, cumulative deviation versus the bound run's `artifacts/equity.csv`) is `reports/window.json` under the book root. A deviation past `VIBE_TRADING_PAPER_BOOK_DEVIATION_THRESHOLD` still clears `tradable` for the rest of the window. Nothing on this path sends a broker order.

## What this is for

- Holding period: days to a few weeks, not seconds.
- Direction: long only. Exits are sells of shares the book already holds (T+1).
- Size: mid and small capital. Default paper cash is `1_000_000`. One name defaults to **20%** of marked NAV (`VIBE_TRADING_PAPER_BOOK_MAX_POSITION`). New buys stop after a **15%** peak-to-mark drawdown (`VIBE_TRADING_PAPER_BOOK_MAX_DRAWDOWN`). An optional buy-side participation cap (`VIBE_TRADING_PAPER_BOOK_MAX_VOLUME_RATIO`) limits one symbol's daily buy shares to a ratio of that bar's volume (0 disables). Sells still settle.
- A-share markers, when the bar or the tradability panel can support them:
  - **limit-up** and **one-word board** (open at the ceiling, low never left it)
  - **ST** (panel `is_st`, a bar flag, or a name starting with `ST` / `*ST`)
  - **consecutive board** (2 limit-up sessions) and **extreme sentiment** (3 or more on this name)

Extreme sentiment here is the name's own streak. A market-wide emotion index is not on the bar panel, so it is not invented.

## Explicit bans

- No full-auto get-rich narrative. A passing gate is permission to **paper-trade**, not a promise of profit. That pass does not review out-of-sample results. OOS not reviewed unless `VIBE_TRADING_VAL_REQUIRE_OUT_OF_SAMPLE=1`.
- No race against institutional HFT or exchange-colocation alpha. The clock is the daily bar.
- No LLM on the auto-order path. The model may help write a hypothesis. It does not approve `tradable`, size the paper book, or place an order. Broker orders stay on the upstream mandate layer, outside this loop.

## Merge gate

Code merges only when the offline CI pytest is green. The command is the same one `.github/workflows/test.yml` runs, and it is written out in `CONTRIBUTING.md`. Live end-to-end tests are outside this gate.

## Research-only (frozen)

These surfaces still load. They are not the default, and they are not evidence a signal is tradable.

- `agent/prop_*.md` — frozen sector and factor prompts
- `agent/scripts/run_f0_f1_*.py`, `agent/scripts/run_sectors_optimal_*.py` — the batch jobs that wrote those prompts
- `agent/src/factors/zoo/custom/` — seven unverified theme factors
- Sector Pick's 「最优」 catalog (`agent/src/sector_pick/catalog.py`) — recipes that point at those prompts. Each row is `research_only`. Sector Pick and Market Pick label those recipes research-only so they do not look like a tradable path.

Market Pick, Sector Pick, and the analog forecast remain available as research boards. On Monitor, rows that have not cleared the journal and the gate sit on a dashed research board; tradable signals sit on a separate lane. A name becomes tradable only through the path above.
