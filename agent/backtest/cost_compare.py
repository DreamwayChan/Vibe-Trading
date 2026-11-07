"""Side-by-side frictionless vs A-share cost+tradability for one strategy.

The production backtest (``python -m backtest.runner``) stays on
``ashare_costs`` / :class:`backtest.engines.china_a.ChinaAEngine`. This module
runs that same strategy, the same bars, and the same window a second time
under :data:`backtest.ashare_costs.FRICTIONLESS_PROFILE` and reports both
metric sets plus ``delta`` = cost-aware − frictionless (the cost tax).

It does not replace ``artifacts/metrics.csv`` or ``run_card.json``. The paired
report is ``artifacts/cost_compare.json``.

::

    python -m backtest.cost_compare <run_dir>
    vibe-trading cost-compare <run_dir>

``POST /runs/{run_id}/cost-compare`` returns the same payload. Monitor
re-bench keeps the single cost-aware engine.

Frictionless knobs (fixed by the named profile; they replace ``config.json``
and ``VIBE_TRADING_ASHARE_*``):

* ``commission_rate=0``, ``commission_min=0``, ``stamp_tax=0``,
  ``transfer_fee=0``, ``slippage=0``
* ``t_plus_one=false`` — same-day sells are allowed
* ``block_suspension=false`` — halts and zero-volume bars may fill
* ``block_st_buy=false`` — ST/*ST names may be bought
* ``enforce_price_limits=false`` — limit-up buys and limit-down sells may
  fill; the tradability-panel band and the close-to-close heuristic are
  both skipped

Short sales stay blocked. Buys still round down to 100-share lots. A
tradability panel may still be attached so both legs see the same object;
the frictionless flags mean it is not applied.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from backtest.ashare_costs import (
    PROFILE_ASHARE_COSTS,
    PROFILE_FRICTIONLESS,
    apply_frictionless,
)
from backtest.engines.china_a import ChinaAEngine
from backtest.runner import RunPrepError, prepare_backtest

_INT_KEYS = ("trade_count", "fill_count", "unfill_count")
_FLOAT_KEYS = (
    "nav",
    "final_value",
    "total_return",
    "max_drawdown",
    "avg_turnover",
    "total_turnover",
)

_FRICTIONLESS_NOTES = (
    "Named profile frictionless. These keys replace config.json and "
    "VIBE_TRADING_ASHARE_*. commission/stamp/transfer/slippage are zero. "
    "t_plus_one is false (same-day sells allowed). block_suspension is false "
    "(halted and zero-volume bars may fill). block_st_buy is false "
    "(ST/*ST names may be bought). enforce_price_limits is false "
    "(limit-up buys and limit-down sells may fill; panel bands and the "
    "close-to-close heuristic are both skipped). Short sales stay blocked. "
    "Buys still round down to 100-share lots. tradability_panel records "
    "whether a panel object was attached; the flags above mean it is not applied."
)
_ASHARE_NOTES = (
    "Production default. ChinaAEngine merges backtest.ashare_costs: explicit "
    "config.json keys win, then VIBE_TRADING_ASHARE_* and "
    "VIBE_TRADING_TRADABILITY_PANEL, then the retail schedule. This comparison "
    "does not change that default. Statistical validation is not re-run; the "
    "cost-aware artifact from python -m backtest.runner remains the gate."
)


class _IsolatedLoader:
    """Return a fresh frame copy on every fetch so the two legs cannot alias."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        data = self._inner.fetch(
            codes, start_date, end_date, fields=fields, interval=interval,
        )
        return {code: frame.copy() for code, frame in data.items()}


def _round_metric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 8)


def _metric_row(metrics: dict, engine: ChinaAEngine) -> dict[str, Any]:
    """Key metrics for one leg. ``nav`` is ending equity (``final_value``)."""
    nav = _round_metric(metrics.get("final_value"))
    row: dict[str, Any] = {
        "nav": nav,
        "final_value": nav,
        "total_return": _round_metric(metrics.get("total_return")),
        "max_drawdown": _round_metric(metrics.get("max_drawdown")),
        "avg_turnover": _round_metric(metrics.get("avg_turnover")),
        "total_turnover": _round_metric(metrics.get("total_turnover")),
        "trade_count": int(metrics.get("trade_count") or 0),
        "fill_count": int(engine.fill_count),
        "unfill_count": int(engine.unfill_count),
    }
    return row


def _delta(cost: dict[str, Any], frictionless: dict[str, Any]) -> dict[str, Any]:
    """cost-aware − frictionless. A negative return is the cost tax."""
    out: dict[str, Any] = {}
    for key in (*_FLOAT_KEYS, *_INT_KEYS):
        left = cost.get(key)
        right = frictionless.get(key)
        if key in _INT_KEYS:
            out[key] = int(left or 0) - int(right or 0)
            continue
        if left is None or right is None:
            out[key] = None
            continue
        out[key] = _round_metric(float(left) - float(right))
    return out


def _knobs(engine: ChinaAEngine) -> dict[str, Any]:
    return {
        "commission_rate": engine.commission_rate,
        "commission_min": engine.commission_min,
        "stamp_tax": engine.stamp_tax,
        "transfer_fee": engine.transfer_fee,
        "slippage": engine.slippage_rate,
        "t_plus_one": bool(engine.t_plus_one),
        "block_suspension": bool(engine.block_suspension),
        "block_st_buy": bool(engine.block_st_buy),
        "enforce_price_limits": bool(engine.enforce_price_limits),
        "tradability_panel": engine.tradability_index is not None,
    }


def _run_leg(
    engine: ChinaAEngine,
    strategy_config: dict,
    loader: Any,
    signal_engine: Any,
    bars_per_year: int | None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="cost-compare-") as scratch:
        return engine.run_backtest(
            dict(strategy_config),
            loader,
            signal_engine,
            Path(scratch),
            bars_per_year,  # type: ignore[arg-type]
            persist=False,
        )


def compare_profiles(
    strategy_config: dict,
    loader: Any,
    signal_engine: Any,
    *,
    engine_config: dict | None = None,
    bars_per_year: int | None = 252,
) -> dict[str, Any]:
    """Run one strategy twice and return both metric sets plus the cost tax.

    Args:
        strategy_config: Window, codes, and cash passed to ``run_backtest``.
            The same object is used for both legs.
        loader: Loader whose ``fetch`` returns the bars. Each leg receives a
            copy of those frames.
        signal_engine: Instance whose ``generate`` is called once per leg.
            It must be deterministic for a given ``data_map``.
        engine_config: Resolved ChinaAEngine config for the cost-aware leg
            (panel included). Defaults to *strategy_config*, which then picks
            up ``ashare_costs`` the same way a hand-built engine does.
        bars_per_year: Annualisation factor shared by both legs. ``None``
            matches a cross-market production run.

    Returns:
        Report dict. ``metrics.delta`` is ashare_costs − frictionless.
    """
    resolved = dict(strategy_config if engine_config is None else engine_config)
    cost_engine = ChinaAEngine(dict(resolved))
    friction_engine = ChinaAEngine(apply_frictionless(resolved))
    isolated = _IsolatedLoader(loader)
    # Cost-aware first so its generate() call matches a single production run
    # when a signal caches the first result on itself.
    cost_metrics = _run_leg(
        cost_engine, strategy_config, isolated, signal_engine, bars_per_year,
    )
    friction_metrics = _run_leg(
        friction_engine, strategy_config, isolated, signal_engine, bars_per_year,
    )
    cost_row = _metric_row(cost_metrics, cost_engine)
    friction_row = _metric_row(friction_metrics, friction_engine)
    return {
        "status": "ok",
        "production_default": PROFILE_ASHARE_COSTS,
        "delta_definition": (
            "ashare_costs - frictionless. Negative total_return or nav is the "
            "cost tax: fees, slippage, and orders the limit, halt, or T+1 "
            "rules refused."
        ),
        "fill_count_includes": (
            "opening fills, signal exits, and the terminal liquidation"
        ),
        "window": {
            "codes": list(strategy_config.get("codes") or []),
            "start_date": strategy_config.get("start_date"),
            "end_date": strategy_config.get("end_date"),
            "interval": strategy_config.get("interval", "1D"),
            "initial_cash": cost_engine.initial_capital,
        },
        "profiles": {
            PROFILE_FRICTIONLESS: {
                "name": PROFILE_FRICTIONLESS,
                "label_zh": "无成本",
                "label_en": "frictionless",
                "knobs": _knobs(friction_engine),
                "notes": _FRICTIONLESS_NOTES,
            },
            PROFILE_ASHARE_COSTS: {
                "name": PROFILE_ASHARE_COSTS,
                "label_zh": "成本+涨跌停",
                "label_en": "cost + tradability",
                "knobs": _knobs(cost_engine),
                "notes": _ASHARE_NOTES,
            },
        },
        "metrics": {
            PROFILE_FRICTIONLESS: friction_row,
            PROFILE_ASHARE_COSTS: cost_row,
            "delta": _delta(cost_row, friction_row),
        },
    }


def write_cost_compare(path: Path, report: dict[str, Any]) -> None:
    """Write the report. Does not touch metrics.csv or the run card."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def compare_run_dir(run_dir: Path, *, write: bool = True) -> dict[str, Any]:
    """Load *run_dir* once and compare the two A-share profiles.

    Raises:
        RunPrepError: The run is missing, not an A-share cash book, or the
            production loader/signal path failed.
    """
    prepared = prepare_backtest(run_dir)
    if prepared.engine_type == "options":
        raise RunPrepError(
            "cost comparison is only defined for the A-share cash engine"
        )
    from backtest.runner import _create_market_engine

    probe = _create_market_engine(
        prepared.effective_source, prepared.config, prepared.codes,
    )
    if not isinstance(probe, ChinaAEngine):
        raise RunPrepError(
            "cost comparison requires an A-share book filled by ChinaAEngine"
        )
    report = compare_profiles(
        prepared.config,
        prepared.loader,
        prepared.signal_engine,
        engine_config=probe.config,
        bars_per_year=prepared.bars_per_year,
    )
    if write:
        write_cost_compare(prepared.run_dir / "artifacts" / "cost_compare.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI: print the report and write ``artifacts/cost_compare.json``."""
    parser = argparse.ArgumentParser(
        prog="python -m backtest.cost_compare",
        description=(
            "Run one strategy twice on the same bars and window: the named "
            "frictionless profile versus the production A-share cost and "
            "tradability engine. Prints both metric sets and delta "
            "(ashare_costs - frictionless). Does not change the default backtest."
        ),
    )
    parser.add_argument(
        "run_dir",
        help="Run directory with config.json and code/signal_engine.py",
    )
    args = parser.parse_args(argv)
    try:
        report = compare_run_dir(Path(args.run_dir))
    except RunPrepError as exc:
        print(json.dumps({"error": exc.message}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
