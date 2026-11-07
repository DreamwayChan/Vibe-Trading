"""Screen Eastmoney boards with the dual-track rule; one OHLCV pull for unique names."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from functools import partial

import pandas as pd

from src.data.lake_fetch import FetchStats, fetch_lake_first
from src.market_pick.boards import (
    BoardUniverse,
    load_board_universe,
    member_coverage,
    unique_symbols,
)
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


def _settled_end(as_of_date: date, *, today: date | None = None) -> date:
    """Last bar date the loader cache will keep on disk.

    ``loader_cache_put`` refuses a range ending today (its last bar is still
    forming) and the cache key is content-addressed on (start, end), so asking
    for ``end=today`` meant nothing was ever written: every cold start — and
    every server restart — re-downloaded the whole universe. At ~4600 symbols
    over a 400-day window that is ~230 batched ``daily`` calls, which is
    exactly what kept tripping Tushare's per-minute cap.

    Fetching through the previous *weekday* instead makes the window stable
    (Saturday, Sunday and Monday all resolve to Friday), while
    ``_apply_same_day_overlay`` still patches today's provisional bar from live
    quotes. A historical ``as_of`` is already settled and is left untouched.
    """
    today = today if today is not None else date.today()
    if as_of_date < today:
        return as_of_date
    settled = today - timedelta(days=1)
    while settled.weekday() >= 5:
        settled -= timedelta(days=1)
    return settled


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

    Bars come from the data lake when it covers the requested window, and only
    the gaps are pulled from a loader (see :func:`_default_fetch`). ``boards`` /
    ``fetch_ohlcv`` are injected by tests so a unit scan never hits Eastmoney,
    Tushare or the lake.
    """
    as_of_date = as_of or date.today()
    universe = boards if boards is not None else load_board_universe(
        as_of_date, refresh_lists=refresh_lists
    )
    symbols = unique_symbols(universe)
    # Anchor both ends on the settled date: the loader cache key is
    # content-addressed on (start, end), so deriving ``start`` from today would
    # give Saturday, Sunday and Monday three different keys and miss every time.
    settled_end = _settled_end(as_of_date)
    start = (settled_end - timedelta(days=max(30, lookback_days))).isoformat()
    end = settled_end.isoformat()
    # Filled in place by ``_default_fetch``; stays empty when a test injects its
    # own fetcher, which is why ``bars_source`` is None rather than a guess.
    fetch_stats = FetchStats()
    fetcher = fetch_ohlcv or partial(_default_fetch, stats=fetch_stats)
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
    coverage = member_coverage(universe)
    degraded = _degraded_reason(universe, coverage)
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
        member_coverage=round(coverage, 4),
        degraded_reason=degraded,
        bars_source=fetch_stats.label(),
        boards=rows,
        horizons=horizons,
    )


_COVERAGE_OK = 0.98


def _degraded_reason(
    universe: list[BoardUniverse],
    coverage: float,
) -> str | None:
    """Plain-language note when the screen rests on a partial board universe.

    Member lists are filled by a slow background warm-up (Eastmoney bans fast
    bursts), so a cold cache legitimately covers only part of the boards. The
    ranking is still usable, but the UI must not present it as the full market.
    """
    if coverage >= _COVERAGE_OK:
        return None
    missing = sum(1 for board in universe if not board.symbols)
    return (
        f"板块成分覆盖 {coverage:.0%}（{missing}/{len(universe)} 个板块待补全），"
        "后台正在低频率补拉，完成后重新扫描即为全市场结果。"
    )


def _default_fetch(
    symbols: list[str],
    start: str,
    end: str,
    source: str,
    interval: str,
    *,
    stats: FetchStats | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Serve the screen from the data lake, closing only gaps from the network.

    The lake is the system of record for settled daily bars: ingestion fills it
    once, and every consumer reads it instead of re-downloading. Before this,
    the screen pulled the whole 400-day window from the loader on every new
    session — ~4600 names, ~230 batched ``daily`` calls — even when the data
    centre had written the same bars minutes earlier.

    The split/splice rules live in :mod:`src.data.lake_fetch` so the monitor
    board and this screen cannot drift apart. ``stats`` is filled in place so
    the caller can stamp the result with where its bars came from.
    """
    data_map, errors, _ = fetch_lake_first(
        symbols,
        start=start,
        end=end,
        source=source,
        interval=interval,
        network=_fetch_universe_ohlcv,
        label="market-pick",
        stats=stats,
    )
    return data_map, errors


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
