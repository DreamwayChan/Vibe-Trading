"""Score enabled catalog sectors, then load holdings for a locked theme book.

OHLCV fetch and same-day quote overlay reuse the monitor scan helpers so live
bars stay consistent with the per-strategy board. Scoring uses EW/MA20 +
confirm coverage. Rank-1 is the observation map. The persisted ThemeLock is
the order: a healthy book is kept until it fails the leftover-book gate, or
a challenger leads for enough days with a real score gap. Index bear still
ticks that streak (and may switch the lock key) but does not open a book.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.api.helpers import RUNS_DIR
from src.monitor.models import DayOps, HoldingRow, MonitorConfig, StrategyRef
from src.monitor.scan import (
    MonitorScanError,
    _apply_same_day_overlay,
    _fetch_universe_ohlcv,
    _run_source_interval,
    run_monitor_scan,
)
from src.monitor.trend_regime import (
    CSI300,
    classify_structure,
    horizons_from_close,
    stamp_holding_trends,
    summarize_panel,
)
from src.monitor.universe import load_run_universe
from src.sector_pick.catalog import CATALOG, SectorRecipe, recipe_by_key
from src.sector_pick.models import SectorPickResult, SectorScore
from src.sector_pick.theme_lock import (
    CHALLENGER_CONFIRM_DAYS,
    ThemeLock,
    advance_lock_identity,
    lock_row_is_eligible,
    row_by_key,
    should_steal,
    tick_challenger,
)
from src.sector_pick.score import (
    apply_regime_stance,
    assign_live_scores,
    build_reason,
    build_untradable_reason,
    close_panel,
    confirm_pass_rate,
    historical_metrics,
    load_confirm_irs,
    ranked_candidates,
    smooth_cs,
    stamp_book,
    timing_and_momentum,
)

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_DAYS = 400


class SectorPickError(ValueError):
    """User-correctable sector-pick configuration error."""


def run_sector_pick_scan(
    enabled_keys: list[str],
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    runs_root: Path | None = None,
    holdings_key: str | None = None,
    skip_holdings: bool = False,
    theme_lock: ThemeLock | dict | None = None,
) -> SectorPickResult:
    """Score every enabled catalog row and optionally load one sector's holdings.

    Args:
        enabled_keys: Catalog keys the user opted in (already validated).
        holdings_key: When set, load this sector's engine holdings instead of
            the locked book. Used by the "preview this board" action; does not
            mutate the persisted lock.
        skip_holdings: Score only (tests / when the engine is unavailable).
        theme_lock: Previous day's locked theme. Rank-1 is the map; the lock
            stays until the book fails or a challenger earns a switch.
    """
    as_of_date = as_of or date.today()
    root = runs_root or RUNS_DIR
    enabled = {str(key).strip() for key in enabled_keys if str(key).strip()}
    irs = load_confirm_irs()
    source, interval = _source_interval_for_enabled(enabled, root)

    # One CSI 300 pull for the whole scan. Failure leaves F0 scores unchanged.
    market_close, market_regime, mom120 = _fetch_csi300_close(
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        source=source,
        interval=interval,
    )
    horizons = horizons_from_close(market_close)

    rows: list[SectorScore] = []
    for recipe in CATALOG:
        if recipe.key not in enabled:
            rows.append(_disabled_row(recipe))
            continue
        try:
            rows.append(
                score_recipe(
                    recipe,
                    as_of_date=as_of_date,
                    lookback_days=lookback_days,
                    runs_root=root,
                    irs=irs,
                    market_close=market_close,
                )
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft per sector
            logger.warning("sector-pick score failed for %s: %s", recipe.key, exc)
            rows.append(_error_row(recipe, str(exc)))

    winner, index_stance = assign_live_scores(rows, market_regime=market_regime)
    map_key = winner.key if winner is not None else None
    incoming = ThemeLock.from_dict(
        theme_lock.to_dict() if isinstance(theme_lock, ThemeLock) else theme_lock
    )
    as_of_s = as_of_date.isoformat()
    stance = index_stance
    reason = build_reason(winner, stance, market_regime=market_regime, horizons=horizons)
    # Index bear: rank for display, do not open a new book. Observation still ticks.
    allow_new_book = market_regime != "bear" and stance != "none"

    preview_key = (holdings_key or "").strip()
    holdings: list[HoldingRow] = []
    ops = DayOps()
    holdings_run_id: str | None = None
    overlay_provisional = False
    recommended_key = incoming.locked_key or map_key
    display_key = preview_key or recommended_key
    next_lock = incoming
    lock_event = "held"
    lock_reason = ""

    if skip_holdings:
        # Ranking-only: do not mutate the lock or pretend a book was probed.
        recommended_key = map_key
        display_key = map_key
        lock_event = "skip_holdings"
        lock_reason = "仅评分，不更新锁定主题。"
    elif preview_key:
        # Preview path: load this board only; the persisted lock does not move.
        recipe = recipe_by_key(preview_key)
        if recipe is None:
            raise SectorPickError(f"unknown sector key: {preview_key!r}")
        try:
            holdings, ops, overlay_provisional = _load_holdings(
                recipe,
                as_of_date=as_of_date,
                lookback_days=lookback_days,
                runs_root=root,
            )
            holdings_run_id = recipe.run_id
            preview_row = next((row for row in rows if row.key == preview_key), None)
            if preview_row is not None:
                stamp_book(preview_row, holdings)
        except MonitorScanError as exc:
            logger.warning("sector-pick holdings scan failed for %s: %s", preview_key, exc)
            raise SectorPickError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("sector-pick holdings crashed for %s: %s", preview_key, exc)
            raise SectorPickError(f"holdings scan failed: {exc}") from exc
        recommended_key = incoming.locked_key or map_key
        display_key = preview_key
        lock_event = "preview"
        lock_reason = "预览持仓，不更换锁定主题。"
    elif not allow_new_book:
        # Do not load/open a book, but keep observing the map. Freezing the
        # whole ThemeLock here left challenger_days stuck on the last
        # non-bear scan (e.g. 1/2) while rank-1 had already led for days.
        display_key = None
        holdings = []
        ops = DayOps()
        holdings_run_id = None
        overlay_provisional = False
        stance = "none"
        reason = build_reason(winner, "none", market_regime=market_regime, horizons=horizons)
        if incoming.locked_key:
            locked_row = row_by_key(rows, incoming.locked_key)
            next_lock = advance_lock_identity(
                incoming, map_row=winner, locked_row=locked_row, as_of=as_of_s
            )
            recommended_key = next_lock.locked_key
            shown = row_by_key(rows, next_lock.locked_key) or locked_row
            locked_name = (
                shown.name
                if shown is not None
                else (next_lock.locked_key or incoming.locked_key)
            )
            reason = f"{reason}锁定主题 {locked_name} 今日不加仓。"
            if shown is not None:
                reason = _append_lock_reason(
                    reason, chosen=shown, map_row=winner, lock=next_lock
                )
            if next_lock.locked_key != incoming.locked_key:
                lock_event = "switched_hysteresis"
                lock_reason = f"挑战者 {locked_name} 连续领先，换锁定主题。今日不加仓。"
            else:
                lock_event = "held_bear"
                lock_reason = "指数中期偏空：今天先别买新的，锁定主题保留，不是清仓。"
        else:
            recommended_key = None
            lock_event = "wait_bear"
            lock_reason = "指数中期偏空：没有锁定主题，今天先别买新的。"
    else:
        chosen, holdings, ops, overlay_provisional, next_lock, lock_event, lock_reason = (
            _select_locked_book(
                rows,
                lock=incoming,
                map_row=winner,
                as_of=as_of_s,
                as_of_date=as_of_date,
                lookback_days=lookback_days,
                runs_root=root,
            )
        )
        if chosen is not None:
            recommended_key = chosen.key
            display_key = chosen.key
            holdings_run_id = chosen.run_id
            timing_stance = "full" if (chosen.timing_mult or 0.0) >= 1.0 else "half_watch"
            stance = apply_regime_stance(timing_stance, market_regime)
            reason = build_reason(
                chosen, stance, index_leader=winner, market_regime=market_regime, horizons=horizons
            )
            reason = _append_lock_reason(reason, chosen=chosen, map_row=winner, lock=next_lock)
        else:
            # Index may still be above MA20; leftover books are not a pick.
            recommended_key = None
            display_key = None
            holdings = []
            ops = DayOps()
            holdings_run_id = None
            overlay_provisional = False
            leftover_stance = "half_watch" if index_stance == "full" else index_stance
            stance = apply_regime_stance(leftover_stance, market_regime)
            reason = build_untradable_reason(winner, market_regime=market_regime, horizons=horizons)

    if holdings:
        holdings = attach_holding_trends(
            holdings,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            market_close=market_close,
            source=source,
            interval=interval,
        )

    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = SectorPickResult(
        as_of=as_of_s,
        scanned_at=scanned_at,
        recommended_key=recommended_key,
        holdings_key=display_key if holdings_run_id else recommended_key,
        stance=stance,
        reason=reason,
        sectors=rows,
        holdings=holdings,
        ops=ops,
        holdings_run_id=holdings_run_id,
        provisional=overlay_provisional,
        market_regime=market_regime,
        mom120=mom120,
        locked_key=next_lock.locked_key,
        map_key=map_key,
        challenger_key=next_lock.challenger_key,
        challenger_days=next_lock.challenger_days,
        challenger_as_of=next_lock.challenger_as_of,
        lock_event=lock_event,
        lock_reason=lock_reason,
        horizons=horizons,
    )
    from src.process.attach import decision_for_scan, implied_book_stance

    decision_stance = stance
    if market_regime == "bear":
        decision_stance = implied_book_stance(holdings, market_regime, horizons)
    result.decision = decision_for_scan(
        market_regime=market_regime,
        f0_stance=decision_stance,
        holdings=holdings,
        provisional=overlay_provisional,
        as_of=as_of_s,
        overlays_reason=reason,
        extra_overlays={
            "lock": next_lock.to_dict(),
            "map_key": map_key,
            "lock_event": lock_event,
            "lock_reason": lock_reason,
        },
        horizons=horizons,
    )
    return result


def score_recipe(
    recipe: SectorRecipe,
    *,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame] | None = None,
    market_close: pd.Series | None = None,
) -> SectorScore:
    """Score one catalog sector. ``data_map`` is injected by tests."""
    run_dir = runs_root / recipe.run_id
    excess, sharpe = historical_metrics(run_dir)
    base = SectorScore(
        key=recipe.key,
        name=recipe.name,
        panel=recipe.panel,
        run_id=recipe.run_id,
        variant=recipe.variant,
        enabled=True,
        excess_return=excess,
        sharpe=sharpe,
    )
    if data_map is None:
        if not run_dir.is_dir():
            base.error = f"run not found: {recipe.run_id}"
            return base
        data_map, fetch_errors = _fetch_universe(
            run_dir,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
        )
        base.universe_size = len(load_run_universe(run_dir))
        base.fetch_error_count = len(fetch_errors)
    else:
        base.universe_size = len(data_map)

    close = close_panel(data_map)
    if close.empty:
        base.error = "no OHLCV close panel"
        return base

    metrics = timing_and_momentum(close)
    base.timing_mult = metrics["timing_mult"]
    base.ew_ma20_ratio = metrics["ew_ma20_ratio"]
    base.ret_20d = metrics["ret_20d"]
    if base.timing_mult is None or base.ret_20d is None:
        base.error = "not enough bars for MA20 / 20-day return"
        return base

    try:
        factor_scores = _compute_confirm_scores(data_map, recipe.confirm_ids)
        rate = confirm_pass_rate(close, recipe.confirm_ids, factor_scores, irs)
        base.confirm_pass_rate = rate
    except Exception as exc:  # noqa: BLE001 — timing-only score is still usable
        logger.info("sector-pick confirm skipped for %s: %s", recipe.key, exc)
        base.confirm_pass_rate = None

    if market_close is not None:
        summary = summarize_panel(close, market_close)
        base.rs_n = summary.rs_n
        base.dip_n = summary.dip_n
        base.knife_n = summary.knife_n
        base.wait_n = summary.wait_n
        base.sell_n = summary.sell_n
        base.rs_share = summary.rs_share
        base.knife_share = summary.knife_share
    return base


def _append_lock_reason(
    reason: str,
    *,
    chosen: SectorScore,
    map_row: SectorScore | None,
    lock: ThemeLock,
) -> str:
    """Hero copy: map vs order, plus challenger streak when sesame is leading."""
    bits: list[str] = []
    if map_row is not None and map_row.key != chosen.key:
        bits.append(f"锁定续持 {chosen.name}；观察第一 {map_row.name}")
    if lock.challenger_key and lock.challenger_days > 0:
        chal = map_row if map_row is not None and map_row.key == lock.challenger_key else None
        chal_name = chal.name if chal is not None else lock.challenger_key
        bits.append(
            f"挑战者 {chal_name} 已领先 {lock.challenger_days}/{CHALLENGER_CONFIRM_DAYS} 日"
        )
    if not bits:
        return reason
    return f"{reason}。" + "；".join(bits)


def _select_locked_book(
    rows: list[SectorScore],
    *,
    lock: ThemeLock,
    map_row: SectorScore | None,
    as_of: str,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
) -> tuple[SectorScore | None, list[HoldingRow], DayOps, bool, ThemeLock, str, str]:
    """Probe the locked theme first; rank-walk only after failure.

    Returns (chosen, holdings, ops, provisional, next_lock, event, reason).
    """
    empty_ops = DayOps()
    locked_row = row_by_key(rows, lock.locked_key)

    if lock_row_is_eligible(locked_row) and locked_row is not None:
        probed = _probe_row(
            locked_row,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            runs_root=runs_root,
        )
        if probed is not None and locked_row.book_healthy:
            holdings, ops, provisional = probed
            next_lock = tick_challenger(
                lock, map_row=map_row, locked_row=locked_row, as_of=as_of
            )
            if should_steal(next_lock, map_row=map_row, locked_row=locked_row):
                steal_row = row_by_key(rows, next_lock.challenger_key)
                steal_probed = (
                    _probe_row(
                        steal_row,
                        as_of_date=as_of_date,
                        lookback_days=lookback_days,
                        runs_root=runs_root,
                    )
                    if steal_row is not None
                    else None
                )
                if (
                    steal_probed is not None
                    and steal_row is not None
                    and steal_row.book_healthy
                ):
                    steal_holdings, steal_ops, steal_prov = steal_probed
                    return (
                        steal_row,
                        steal_holdings,
                        steal_ops,
                        steal_prov,
                        ThemeLock(locked_key=steal_row.key),
                        "switched_hysteresis",
                        f"挑战者 {steal_row.name} 连续领先，换锁定主题。",
                    )
                # Sesame led on the map but its engine book is leftover — keep watermelon.
                return (
                    locked_row,
                    holdings,
                    ops,
                    provisional,
                    lock.cleared_challenger(),
                    "held",
                    f"锁定续持 {locked_row.name}；挑战者账本不可执行，不换。",
                )
            return (
                locked_row,
                holdings,
                ops,
                provisional,
                next_lock,
                "held",
                f"锁定续持 {locked_row.name}。",
            )

    chosen, holdings, ops, provisional = _pick_tradable_book(
        rows,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        runs_root=runs_root,
    )
    if chosen is not None:
        event = "switched_failure" if lock.locked_key else "initialized"
        if event == "switched_failure":
            reason = f"锁定主题不可执行，已换 {chosen.name}。"
        else:
            reason = f"初始化锁定主题 {chosen.name}。"
        return chosen, holdings, ops, provisional, ThemeLock(locked_key=chosen.key), event, reason

    event = "released" if lock.locked_key else "empty"
    reason = (
        "锁定主题不可执行，且没有可替换账本。"
        if lock.locked_key
        else "没有可执行账本。"
    )
    return None, [], empty_ops, False, ThemeLock(), event, reason


def _probe_row(
    row: SectorScore,
    *,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
) -> tuple[list[HoldingRow], DayOps, bool] | None:
    """Load one engine book and stamp health. None when the load itself failed."""
    recipe = recipe_by_key(row.key)
    if recipe is None:
        row.skip_reason = f"unknown sector key: {row.key!r}"
        row.book_healthy = False
        return None
    try:
        holdings, ops, provisional = _load_holdings(
            recipe,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            runs_root=runs_root,
        )
    except MonitorScanError as exc:
        logger.warning("sector-pick holdings scan failed for %s: %s", row.key, exc)
        row.skip_reason = f"holdings unavailable: {exc}"
        row.book_healthy = False
        if not row.error:
            row.error = f"holdings unavailable: {exc}"
        return None
    except Exception as exc:  # noqa: BLE001 — caller may walk the next ranked board
        logger.warning("sector-pick holdings crashed for %s: %s", row.key, exc)
        row.skip_reason = f"holdings scan failed: {exc}"
        row.book_healthy = False
        return None
    stamp_book(row, holdings)
    return list(holdings), ops, provisional


def _pick_tradable_book(
    rows: list[SectorScore],
    *,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
) -> tuple[SectorScore | None, list[HoldingRow], DayOps, bool]:
    """Walk live rank order and return the first healthy strategy book.

    Residual books (too few names, or one name dominating) stay ranked for
    display but cannot become the recommended pick. Already-probed residual
    rows (the failed lock) are not fetched a second time.
    """
    empty: tuple[SectorScore | None, list[HoldingRow], DayOps, bool] = (
        None,
        [],
        DayOps(),
        False,
    )
    for row in ranked_candidates(rows):
        # Failed lock was already probed; do not fetch that leftover book again.
        if row.book_healthy is False:
            continue
        probed = _probe_row(
            row,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            runs_root=runs_root,
        )
        if probed is None:
            continue
        holdings, ops, provisional = probed
        if row.book_healthy:
            return row, holdings, ops, provisional
        logger.info(
            "sector-pick skipped residual book %s: %s",
            row.key,
            row.skip_reason,
        )
    return empty


def load_sector_holdings(
    key: str,
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    runs_root: Path | None = None,
) -> tuple[list[HoldingRow], DayOps, str, bool]:
    """Run one catalog sector's SignalEngine and return holdings (no rescoring)."""
    recipe = recipe_by_key(key)
    if recipe is None:
        raise SectorPickError(f"unknown sector key: {key!r}")
    as_of_date = as_of or date.today()
    root = runs_root or RUNS_DIR
    try:
        holdings, ops, provisional = _load_holdings(
            recipe,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            runs_root=root,
        )
    except MonitorScanError as exc:
        raise SectorPickError(str(exc)) from exc
    source, interval = _run_source_interval(root / recipe.run_id) if (root / recipe.run_id).is_dir() else ("auto", "1D")
    holdings = attach_holding_trends(
        holdings,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        source=source,
        interval=interval,
    )
    return holdings, ops, recipe.run_id, provisional


def attach_holding_trends(
    holdings: list[HoldingRow],
    *,
    as_of_date: date,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    market_close: pd.Series | None = None,
    source: str = "auto",
    interval: str = "1D",
) -> list[HoldingRow]:
    """Stamp optional ``trend`` on each row and sort hold → … → reduce.

    Fail-soft: a missing CSI 300 bar or a short name leaves ``trend`` unset.
    Does not overwrite ``HoldingRow.action`` (enter/trim).
    """
    if not holdings:
        return holdings
    close = market_close
    if close is None:
        close, _regime, _mom = _fetch_csi300_close(
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            source=source,
            interval=interval,
        )
    if close is None:
        return holdings

    symbols = [row.symbol for row in holdings if row.symbol]
    data_map: dict[str, pd.DataFrame] = {}
    try:
        data_map, _errors = _fetch_closes(
            symbols,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            source=source,
            interval=interval,
        )
    except Exception as exc:  # noqa: BLE001 — holdings still display without trend
        logger.warning("sector-pick holding trend fetch failed: %s", exc)
        return holdings

    return stamp_holding_trends(holdings, data_map, close)


def _fetch_csi300_close(
    *,
    as_of_date: date,
    lookback_days: int,
    source: str,
    interval: str,
) -> tuple[pd.Series | None, str | None, float | None]:
    """One CSI 300 series for the scan. None triple when the pull fails."""
    try:
        data_map, errors = _fetch_closes(
            [CSI300],
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            source=source,
            interval=interval,
        )
    except Exception as exc:  # noqa: BLE001 — overlay is optional
        logger.warning("sector-pick CSI 300 fetch failed: %s", exc)
        return None, None, None
    frame = data_map.get(CSI300)
    if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
        logger.warning(
            "sector-pick CSI 300 missing: %s",
            errors.get(CSI300) or "no OHLCV bars returned",
        )
        return None, None, None
    series = frame["close"].dropna()
    snap = classify_structure(series)
    if snap is None:
        return series, None, None
    return series, snap.regime, round(float(snap.mom120), 4)


def _fetch_closes(
    symbols: list[str],
    *,
    as_of_date: date,
    lookback_days: int,
    source: str,
    interval: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    end = as_of_date.isoformat()
    start = (as_of_date - timedelta(days=max(30, lookback_days))).isoformat()
    data_map, fetch_errors = _fetch_universe_ohlcv(
        symbols,
        start=start,
        end=end,
        source=source,
        interval=interval,
    )
    if data_map:
        data_map, _provisional, _stats = _apply_same_day_overlay(
            data_map, as_of_date=as_of_date
        )
    return data_map, fetch_errors


def _source_interval_for_enabled(enabled: set[str], runs_root: Path) -> tuple[str, str]:
    """Reuse the first enabled run's loader so CSI 300 matches that board."""
    for recipe in CATALOG:
        if recipe.key not in enabled:
            continue
        run_dir = runs_root / recipe.run_id
        if run_dir.is_dir():
            return _run_source_interval(run_dir)
    return "auto", "1D"


def _disabled_row(recipe: SectorRecipe) -> SectorScore:
    return SectorScore(
        key=recipe.key,
        name=recipe.name,
        panel=recipe.panel,
        run_id=recipe.run_id,
        variant=recipe.variant,
        enabled=False,
    )


def _error_row(recipe: SectorRecipe, message: str) -> SectorScore:
    return SectorScore(
        key=recipe.key,
        name=recipe.name,
        panel=recipe.panel,
        run_id=recipe.run_id,
        variant=recipe.variant,
        enabled=True,
        error=message,
    )


def _fetch_universe(
    run_dir: Path,
    *,
    as_of_date: date,
    lookback_days: int,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Load the run universe, fetch OHLCV, overlay same-day live quotes."""
    universe = load_run_universe(run_dir)
    if not universe:
        raise SectorPickError(
            f"strategy universe is empty for {run_dir.name}; "
            "set codes in config.json"
        )
    source, interval = _run_source_interval(run_dir)
    end = as_of_date.isoformat()
    start = (as_of_date - timedelta(days=max(30, lookback_days))).isoformat()

    # Same batched Tushare path as the monitor board. A thread pool of per-symbol
    # calls shared one HTTP session and produced ConnectionResetError 10054.
    data_map, fetch_errors = _fetch_universe_ohlcv(
        universe,
        start=start,
        end=end,
        source=source,
        interval=interval,
    )

    if data_map:
        data_map, _provisional, _stats = _apply_same_day_overlay(
            data_map, as_of_date=as_of_date
        )
    return data_map, fetch_errors


def _compute_confirm_scores(
    data_map: dict[str, pd.DataFrame],
    confirm_ids: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    """Compute 5-day smoothed CS ranks for the sector's confirm legs."""
    if not confirm_ids:
        return {}
    from src.factors.registry import Registry

    panel = _factor_panel(data_map)
    if panel["close"].empty:
        return {}
    registry = Registry()
    scores: dict[str, pd.DataFrame] = {}
    for aid in confirm_ids:
        raw = registry.compute(aid, panel)
        if raw is None or getattr(raw, "empty", True):
            continue
        aligned = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = smooth_cs(aligned)
    return scores


def _factor_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """OHLCV panel plus synthetic amount=close*volume for amount-gated confirms."""
    cols = [c for c in sorted(data_map) if "close" in data_map[c].columns]
    close = pd.DataFrame({c: data_map[c]["close"] for c in cols}).sort_index()

    def _col(name: str) -> pd.DataFrame:
        frame = pd.DataFrame(
            {c: data_map[c][name] for c in cols if name in data_map[c].columns}
        )
        return frame.reindex(index=close.index, columns=close.columns)

    volume = _col("volume")
    amount = close * volume.fillna(0.0)
    return {
        "close": close,
        "open": _col("open"),
        "high": _col("high"),
        "low": _col("low"),
        "volume": volume,
        "amount": amount,
    }


def _load_holdings(
    recipe: SectorRecipe,
    *,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
) -> tuple[list[HoldingRow], DayOps, bool]:
    """Run the bound SignalEngine over the full universe; ignore watchlist."""
    config = MonitorConfig(
        symbols=[],
        strategy=StrategyRef(type="run_dir", run_id=recipe.run_id),
    )
    updated = run_monitor_scan(
        config,
        as_of=as_of_date,
        lookback_days=lookback_days,
        runs_root=runs_root,
        refresh_report=False,
    )
    provisional = any(row.provisional for row in updated.holdings)
    return list(updated.holdings), updated.ops, provisional
