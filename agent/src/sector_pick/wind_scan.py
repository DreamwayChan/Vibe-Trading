"""Historical validation of the sector wind detector on catalog sectors.

Research-only. For each enabled sector it loads the run universe through the
same data-centre path as the sector scan, computes the wind score and checks
whether it predicted T+1-executable excess return over CSI 300. A sector that
fails validation is reported as such and must not feed factor search.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.api.helpers import RUNS_DIR
from src.monitor.trend_regime import CSI300
from src.monitor.universe import load_run_universe
from src.sector_pick.catalog import CATALOG
from src.sector_pick.scan import _fetch_bars, _run_source_interval
from src.sector_pick.wind import detect_sector_wind, validate_wind_predictiveness

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_DAYS = 700


def _wide(data_map: dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    cols = {c: f[col] for c, f in data_map.items() if f is not None and not f.empty and col in f.columns}
    return pd.DataFrame(cols).sort_index()


def evaluate_sector_wind(
    data_map: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    horizon: int = 5,
) -> dict:
    """Pure evaluation on already-loaded bars (testable without network)."""
    close = _wide(data_map, "close")
    opens = _wide(data_map, "open")
    if close.shape[1] < 3:
        return {"status": "insufficient_members", "members": int(close.shape[1])}
    if "amount" in next(iter(data_map.values())).columns:
        amount = _wide(data_map, "amount")
    else:
        amount = _wide(data_map, "volume") * close
    market_amount = None
    if "amount" in benchmark.columns:
        market_amount = benchmark["amount"]
    elif "volume" in benchmark.columns:
        market_amount = benchmark["volume"] * benchmark["close"]

    wind = detect_sector_wind(close, amount, market_amount)
    # equal-weight member open series, rebased so ratios are meaningful
    sector_open = (opens / opens.bfill().iloc[0]).mean(axis=1)
    result = validate_wind_predictiveness(
        wind["score"], sector_open, benchmark["open"].reindex(sector_open.index), horizon=horizon
    )
    last = wind.iloc[-1]
    return {
        "status": "ok",
        "members": int(close.shape[1]),
        "state": str(last["state"]),
        "score": None if pd.isna(last["score"]) else round(float(last["score"]), 3),
        "validation": result,
        "actionable": bool(result["passed"] and last["state"] in ("launch", "main")),
    }


def run_wind_scan(
    keys: list[str] | None = None,
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    horizon: int = 5,
    runs_root: Path | None = None,
) -> dict[str, dict]:
    as_of_date = as_of or date.today()
    root = runs_root or RUNS_DIR
    start = (as_of_date - timedelta(days=lookback_days)).isoformat()
    end = as_of_date.isoformat()
    out: dict[str, dict] = {}
    for recipe in CATALOG:
        if keys and recipe.key not in keys:
            continue
        try:
            run_dir = root / recipe.run_id
            universe = load_run_universe(run_dir)
            if not universe:
                out[recipe.key] = {"status": "no_universe"}
                continue
            source, interval = _run_source_interval(run_dir)
            data_map, _errs = _fetch_bars(universe, start=start, end=end, source=source, interval=interval)
            bench_map, _ = _fetch_bars([CSI300], start=start, end=end, source=source, interval=interval)
            bench = bench_map.get(CSI300)
            if not data_map or bench is None or bench.empty:
                out[recipe.key] = {"status": "no_data"}
                continue
            out[recipe.key] = evaluate_sector_wind(data_map, bench, horizon)
        except Exception as exc:  # noqa: BLE001 — one sector must not stop the scan
            logger.warning("wind scan %s failed: %s", recipe.key, exc)
            out[recipe.key] = {"status": "error", "error": str(exc)}
    return out


def run_wind_cards(
    keys: list[str] | None = None,
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    horizon: int = 5,
    max_factors: int = 40,
    runs_root: Path | None = None,
) -> list[dict]:
    """Wind-gated factor cards for each requested sector (research-only)."""
    from src.factors.registry import Registry
    from src.sector_pick.factor_card import build_sector_cards
    from src.sector_pick.scan import _factor_panel

    as_of_date = as_of or date.today()
    root = runs_root or RUNS_DIR
    start = (as_of_date - timedelta(days=lookback_days)).isoformat()
    end = as_of_date.isoformat()
    registry = Registry()
    out: list[dict] = []
    for recipe in CATALOG:
        if keys and recipe.key not in keys:
            continue
        try:
            run_dir = root / recipe.run_id
            universe = load_run_universe(run_dir)
            source, interval = _run_source_interval(run_dir)
            data_map, _ = _fetch_bars(universe, start=start, end=end, source=source, interval=interval)
            bench_map, _ = _fetch_bars([CSI300], start=start, end=end, source=source, interval=interval)
            bench = bench_map.get(CSI300)
            if not data_map or bench is None or bench.empty:
                out.append({"sector": recipe.key, "status": "no_data", "cards": []})
                continue
            panel = _factor_panel(data_map)
            ids = list(dict.fromkeys([*recipe.confirm_ids, *registry.list()]))[:max_factors]
            factors = {}
            for aid in ids:
                try:
                    f = registry.compute(aid, panel)
                except Exception:  # noqa: BLE001
                    continue
                if f is not None and not getattr(f, "empty", True):
                    factors[aid] = f
            out.append(build_sector_cards(recipe.key, data_map, bench, factors, horizon=horizon))
        except Exception as exc:  # noqa: BLE001
            logger.warning("wind cards %s failed: %s", recipe.key, exc)
            out.append({"sector": recipe.key, "status": "error", "error": str(exc), "cards": []})
    return out
