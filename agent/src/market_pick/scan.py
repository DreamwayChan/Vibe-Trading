"""Screen Eastmoney boards with the dual-track rule; one OHLCV pull for unique names."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src.market_pick.boards import BoardUniverse, load_board_universe, unique_symbols
from src.market_pick.models import BoardScore, MarketPickResult
from src.market_pick.score import (
    assign_board_scores,
    build_reason,
    load_catalog_gates,
    map_leader,
)
from src.monitor.scan import _apply_same_day_overlay, _fetch_universe_ohlcv
from src.monitor.trend_regime import (
    CSI300,
    TrendPlan,
    add_plan_counts,
    classify_structure,
    horizons_from_close,
    plan_from_frames,
)
from src.sector_pick.score import close_panel, timing_and_momentum

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_DAYS = 400

FetchOhlcv = Callable[
    [list[str], str, str, str, str],
    tuple[dict[str, pd.DataFrame], dict[str, str]],
]


class MarketPickError(ValueError):
    """User-correctable market-pick configuration / scan error."""


def run_market_pick_scan(
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    boards: list[BoardUniverse] | None = None,
    fetch_ohlcv: FetchOhlcv | None = None,
    refresh_lists: bool = False,
    source: str = "auto",
    interval: str = "1D",
) -> MarketPickResult:
    """Classify unique constituents once, then aggregate RS/knife per board.

    ``boards`` / ``fetch_ohlcv`` are injected by tests so a unit scan never
    hits Eastmoney or Tushare.
    """
    as_of_date = as_of or date.today()
    universe = boards if boards is not None else load_board_universe(
        as_of_date, refresh_lists=refresh_lists
    )
    symbols = unique_symbols(universe)
    start = (as_of_date - timedelta(days=max(30, lookback_days))).isoformat()
    end = as_of_date.isoformat()
    fetcher = fetch_ohlcv or _default_fetch
    fetch_symbols = list(symbols)
    if CSI300 not in fetch_symbols:
        fetch_symbols.append(CSI300)
    # Tushare daily() is the only batchable A-share path. ``auto`` would start
    # at Tencent and walk thousands of 501/RST errors before reaching it.
    ohlcv_source = source
    if ohlcv_source == "auto":
        try:
            from backtest.loaders.tushare_pool import resolve_tushare_tokens

            if resolve_tushare_tokens():
                ohlcv_source = "tushare"
        except Exception:  # noqa: BLE001 — keep auto if the pool cannot load
            ohlcv_source = source

    data_map, fetch_errors = fetcher(fetch_symbols, start, end, ohlcv_source, interval)
    if data_map:
        try:
            data_map, _, _ = _apply_same_day_overlay(data_map, as_of_date=as_of_date)
        except Exception as exc:  # noqa: BLE001 — classify on T-1 bars
            logger.warning("market-pick live overlay skipped: %s", exc)

    market_close, market_regime, mom120 = _market_from_map(data_map, fetch_errors)
    horizons = horizons_from_close(market_close)
    plans = _classify_plans(data_map, market_close, skip={CSI300})

    rows: list[BoardScore] = []
    for board in universe:
        rows.append(_score_board(board, data_map, plans))

    gates = load_catalog_gates(as_of_date.isoformat())
    winner, stance = assign_board_scores(
        rows, market_regime=market_regime, catalog_gates=gates
    )
    map_row = map_leader(rows)
    reason = build_reason(
        winner, stance, market_regime=market_regime, map_row=map_row, horizons=horizons
    )
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recommended = winner.bk if winner is not None and stance != "none" else None
    return MarketPickResult(
        as_of=as_of_date.isoformat(),
        scanned_at=scanned_at,
        market_regime=market_regime,
        mom120=mom120,
        stance=stance,
        reason=reason,
        recommended_bk=recommended,
        map_bk=map_row.bk if map_row is not None else None,
        unique_symbols=len(symbols),
        ohlcv_ok=len([code for code in symbols if code in data_map]),
        fetch_error_count=len(fetch_errors),
        boards=rows,
        horizons=horizons,
    )


def _default_fetch(
    symbols: list[str],
    start: str,
    end: str,
    source: str,
    interval: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    return _fetch_universe_ohlcv(
        symbols, start=start, end=end, source=source, interval=interval
    )


def _market_from_map(
    data_map: dict[str, pd.DataFrame],
    fetch_errors: dict[str, str],
) -> tuple[pd.Series | None, str | None, float | None]:
    frame = data_map.get(CSI300)
    if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
        logger.warning(
            "market-pick CSI 300 missing: %s",
            fetch_errors.get(CSI300) or "no OHLCV bars returned",
        )
        return None, None, None
    series = frame["close"].dropna()
    snap = classify_structure(series)
    if snap is None:
        return series, None, None
    return series, snap.regime, round(float(snap.mom120), 4)


def _classify_plans(
    data_map: dict[str, pd.DataFrame],
    market_close: pd.Series | None,
    *,
    skip: set[str],
) -> dict[str, TrendPlan]:
    """One plan per symbol. Missing CSI 300 → empty dict (no false longs)."""
    if market_close is None:
        return {}
    out: dict[str, TrendPlan] = {}
    for symbol, frame in data_map.items():
        if symbol in skip or frame is None or getattr(frame, "empty", True):
            continue
        if "close" not in frame.columns:
            continue
        plan = plan_from_frames(frame["close"], market_close)
        if plan is not None:
            out[symbol] = plan
    return out


def _score_board(
    board: BoardUniverse,
    data_map: dict[str, pd.DataFrame],
    plans: dict[str, TrendPlan],
) -> BoardScore:
    counts = {
        "classified": 0,
        "rs_n": 0,
        "dip_n": 0,
        "knife_n": 0,
        "wait_n": 0,
        "sell_n": 0,
    }
    for symbol in board.symbols:
        plan = plans.get(symbol)
        if plan is None:
            continue
        add_plan_counts(plan, counts)
    classified = counts["classified"]
    rs_share = (counts["rs_n"] / classified) if classified else None
    knife_share = (counts["knife_n"] / classified) if classified else None
    metrics = _board_ew_metrics(board.symbols, data_map)
    return BoardScore(
        bk=board.bk,
        name=board.name,
        board_type=board.board_type,
        classified=classified,
        universe_size=len(board.symbols),
        rs_n=counts["rs_n"],
        dip_n=counts["dip_n"],
        knife_n=counts["knife_n"],
        wait_n=counts["wait_n"],
        sell_n=counts["sell_n"],
        rs_share=rs_share,
        knife_share=knife_share,
        ret_20d=metrics["ret_20d"],
        timing_mult=metrics["timing_mult"],
        ew_ma20_ratio=metrics["ew_ma20_ratio"],
    )


def _board_ew_metrics(
    symbols: list[str],
    data_map: dict[str, pd.DataFrame],
) -> dict[str, float | None]:
    """Equal-weight MA20 timing and 20-session return for the board.

    Timing is a recommendation gate (below MA20 cannot be the pick). 20-day
    return stays display-only and is not a rank input.
    """
    subset = {
        code: data_map[code]
        for code in symbols
        if code in data_map
    }
    return timing_and_momentum(close_panel(subset))
