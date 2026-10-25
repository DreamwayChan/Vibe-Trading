"""Deterministic monitor scan: bound strategy → suggestions + holdings history.

Default path loads a backtest ``SignalEngine`` over the **full run universe**
(so cross-sectional Top-N weights are correct), maps watchlist names to
buy/sell/hold suggestions, and persists a daily holdings snapshot.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.api.helpers import RUNS_DIR
from src.monitor.holdings import (
    build_holdings_and_ops,
    previous_weights_from_holdings,
    weights_from_signal_map,
)
from src.monitor.models import (
    DayOps,
    MonitorConfig,
    MonitorDaySnapshot,
    MonitorSuggestion,
    StrategyRef,
)
from src.monitor.store import (
    is_safe_history_key,
    load_previous_snapshot,
    save_day_snapshot,
)
from src.monitor.trend_regime import CSI300, horizons_from_close, stamp_holding_trends
from src.monitor.universe import load_run_universe

logger = logging.getLogger(__name__)

# Signal engines often need long lookbacks (MA200, factors). 400 calendar days
# is a safe default window without pulling multi-year panels for a monitor tick.
_DEFAULT_LOOKBACK_DAYS = 400
# CSI 300 tape uses the same span as forecast compare so T-1 parquet keys match
# (index_daily is 1 call/hour; end=today skips disk cache).
_CSI300_LOOKBACK_DAYS = 800
# Match runner: do not per-symbol-retry a failed batch of this size or larger.
_LARGE_UNIVERSE = 40

_ACTION_ORDER = {"buy": 0, "sell": 1, "hold": 2}

# One CSI 300 frame per as-of day. Switching strategy boards must not spend
# another index_daily call — Tushare's hourly quota fail-fasts the rest.
_CSI300_TREND_MEMO: tuple[date, str, str, pd.DataFrame] | None = None


class MonitorScanError(ValueError):
    """User-correctable scan configuration / strategy error."""


def run_monitor_scan(
    config: MonitorConfig,
    *,
    as_of: date | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    runs_root: Path | None = None,
    refresh_report: bool = False,
) -> MonitorConfig:
    """Run one scan; update suggestions, holdings, ops; persist day snapshot.

    Fail-soft per symbol: a single bad ticker becomes a hold/error suggestion
    and does not abort the whole board.

    Args:
        refresh_report: When True (and the env kill-switch allows it), re-bench
            the bound ``run_dir`` through ``as_of`` so RunDetail K-lines /
            buy-sell markers stay current. Default False keeps legacy scan-only
            behaviour; the UI checkbox opts in per click.
    """
    strategy = config.strategy
    as_of_date = as_of or date.today()
    root = runs_root or RUNS_DIR

    if strategy.type == "shadow":
        if not config.symbols:
            raise MonitorScanError("monitor has no symbols; add tickers before scanning")
        suggestions, holdings, ops, snap_meta = _scan_shadow(config, as_of_date)
        # Avoid ":" in path keys (illegal on Windows volume paths).
        run_key = f"shadow_{(strategy.shadow_id or '').strip()}"
    elif strategy.type == "run_dir":
        run_id = (strategy.run_id or "").strip()
        if not run_id:
            raise MonitorScanError("strategy.run_id is required for run_dir scans")
        suggestions, holdings, ops, snap_meta = _scan_run_dir(
            config,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
            runs_root=root,
        )
        run_key = run_id
    else:
        raise MonitorScanError(f"unsupported strategy type: {strategy.type!r}")

    suggestions.sort(key=lambda s: (_ACTION_ORDER.get(s.action, 9), s.symbol))
    scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    as_of_iso = as_of_date.isoformat()

    config.suggestions = suggestions
    config.holdings = holdings
    config.ops = ops
    config.last_scan_at = scanned_at

    from src.process.attach import decision_for_scan, implied_book_stance, market_regime_from_holdings

    regime = market_regime_from_holdings(holdings)
    horizons = None
    if (
        _CSI300_TREND_MEMO is not None
        and _CSI300_TREND_MEMO[0] == as_of_date
        and "close" in getattr(_CSI300_TREND_MEMO[3], "columns", [])
    ):
        horizons = horizons_from_close(_CSI300_TREND_MEMO[3]["close"])
    config.decision = decision_for_scan(
        market_regime=regime,
        f0_stance=implied_book_stance(holdings, regime, horizons),
        holdings=holdings,
        provisional=bool(snap_meta.get("provisional")),
        as_of=as_of_iso,
        horizons=horizons,
    )

    # Persist daily snapshot (same-day overwrite) for history UI / overnight
    # baseline. Intraday writes are display-only: the next same-day scan still
    # diffs against the prior calendar day's holdings, not this file.
    if run_key and is_safe_history_key(run_key):
        snapshot = MonitorDaySnapshot(
            as_of=as_of_iso,
            scanned_at=scanned_at,
            run_id=run_key,
            provisional=bool(snap_meta.get("provisional")),
            holdings=list(holdings),
            ops=ops,
            suggestions=list(suggestions),
            universe_size=int(snap_meta.get("universe_size") or 0),
            fetch_error_count=int(snap_meta.get("fetch_error_count") or 0),
            live_quote_ok=int(snap_meta.get("live_quote_ok") or 0),
            live_quote_total=int(snap_meta.get("live_quote_total") or 0),
            live_quote_coverage=float(snap_meta.get("live_quote_coverage") or 0.0),
            decision=config.decision,
        )
        try:
            save_day_snapshot(snapshot)
        except Exception as exc:  # noqa: BLE001 — board update still succeeds
            logger.warning("monitor day snapshot save failed: %s", exc)

    # Opt-in: keep the bound RunDetail report (K-line / trades / equity) current.
    # Only run_dir strategies have a report; shadow boards and unchecked scans skip.
    # Fail-soft: board suggestions/holdings above already succeeded.
    if (
        refresh_report
        and strategy.type == "run_dir"
        and (strategy.run_id or "").strip()
    ):
        try:
            from src.monitor.report_refresh import refresh_bound_run_report

            refresh_bound_run_report(
                str(strategy.run_id).strip(),
                as_of_date,
                runs_root=root,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the scan on report refresh
            logger.warning(
                "monitor report refresh crashed for %s: %s",
                strategy.run_id,
                exc,
            )

    return config


def _scan_run_dir(
    config: MonitorConfig,
    *,
    as_of_date: date,
    lookback_days: int,
    runs_root: Path,
) -> tuple[list[MonitorSuggestion], list, DayOps, dict[str, Any]]:
    run_id = (config.strategy.run_id or "").strip()
    run_dir = runs_root / run_id
    signal_path = run_dir / "code" / "signal_engine.py"
    if not signal_path.is_file():
        raise MonitorScanError(f"signal_engine.py not found for run {run_id!r}")

    engine = _load_signal_engine(signal_path)
    source, interval = _run_source_interval(run_dir)

    # Full strategy universe for correct cross-sectional weights; watchlist is
    # only used to filter the suggestions table below.
    universe = load_run_universe(run_dir, fallback=config.symbols)
    if not universe:
        raise MonitorScanError(
            "strategy universe is empty; set codes in the run config.json "
            "or add watchlist symbols"
        )

    end = as_of_date.isoformat()
    start = (as_of_date - timedelta(days=max(30, lookback_days))).isoformat()

    # One batched loader call (Tushare packs ~20 A-shares per daily()) instead
    # of N keep-alive round-trips that reset mid-scan on Windows.
    data_map, fetch_errors = _fetch_universe_ohlcv(
        universe,
        start=start,
        end=end,
        source=source,
        interval=interval,
    )

    as_of_iso = as_of_date.isoformat()
    watchlist = {s.upper() for s in config.symbols}
    suggestions: list[MonitorSuggestion] = []

    # Watchlist-only error rows (full-universe fetch errors stay as counts).
    for symbol, err in fetch_errors.items():
        if symbol not in watchlist:
            continue
        suggestions.append(
            MonitorSuggestion(
                symbol=symbol,
                action="hold",
                position="unknown",
                reason=f"data unavailable: {err}",
                as_of=as_of_iso,
            )
        )

    snap_meta: dict[str, Any] = {
        "universe_size": len(universe),
        "fetch_error_count": len(fetch_errors),
        "provisional": False,
    }

    if not data_map:
        holdings, ops = build_holdings_and_ops({}, None)
        return suggestions, holdings, ops, snap_meta

    provisional_by_symbol: dict[str, bool] = {}
    if _is_daily_interval(interval):
        data_map, provisional_by_symbol, overlay_stats = _apply_same_day_overlay(
            data_map, as_of_date=as_of_date
        )
        snap_meta.update(overlay_stats)

    try:
        signal_map = engine.generate(data_map)
    except Exception as exc:
        raise MonitorScanError(f"SignalEngine.generate failed: {exc}") from exc

    if not isinstance(signal_map, dict):
        raise MonitorScanError("SignalEngine.generate must return a dict of Series")

    meta_by_symbol: dict[str, dict] = {}
    for symbol in data_map:
        frame = data_map[symbol]
        bar_as_of = _frame_last_date(frame, as_of_iso)
        last_price = _frame_last_close(frame)
        is_provisional = bool(provisional_by_symbol.get(symbol))
        meta_by_symbol[symbol] = {
            "price": last_price,
            "data_as_of": bar_as_of,
            "provisional": is_provisional,
        }
        if is_provisional:
            snap_meta["provisional"] = True

        # Suggestions stay scoped to the configured watchlist.
        if symbol not in watchlist:
            continue
        series = signal_map.get(symbol)
        if series is None:
            suggestions.append(
                MonitorSuggestion(
                    symbol=symbol,
                    action="hold",
                    position="unknown",
                    reason="strategy returned no signal series for this symbol",
                    as_of=as_of_iso,
                    data_as_of=bar_as_of,
                    price=last_price,
                    provisional=is_provisional,
                )
            )
            continue
        suggestions.append(
            _map_series_to_suggestion(
                symbol,
                series,
                as_of_iso,
                data_as_of=bar_as_of,
                price=last_price,
                provisional=is_provisional,
            )
        )

    current_weights = weights_from_signal_map(signal_map)
    # Always diff against the last holdings scan *before* today. Same-day
    # rescans overwrite today's file for UI only and must not become each
    # other's baseline — each click is a fresh realtime recalculation.
    prev_snap = load_previous_snapshot(run_id, as_of_iso)
    previous_weights = (
        previous_weights_from_holdings(prev_snap.holdings) if prev_snap else None
    )
    holdings, ops = build_holdings_and_ops(
        current_weights,
        previous_weights,
        meta_by_symbol=meta_by_symbol,
    )
    # Dual-track tape action (hold / buy_dip / reduce) — not enter/trim.
    # CSI 300 is fetched after generate so the index never enters the engine.
    holdings = _stamp_holdings_trend(
        holdings,
        data_map,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        source=source,
        interval=interval,
    )
    return suggestions, holdings, ops, snap_meta


def _stamp_holdings_trend(
    holdings: list,
    data_map: dict[str, pd.DataFrame],
    *,
    as_of_date: date,
    lookback_days: int,
    source: str,
    interval: str,
) -> list:
    """Reuse universe OHLCV; pull CSI 300 once per day through T-1 + overlay."""
    if not holdings:
        return holdings
    market_frame = _csi300_frame_for_trends(
        data_map,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        source=source,
        interval=interval,
    )
    if (
        market_frame is None
        or getattr(market_frame, "empty", True)
        or "close" not in getattr(market_frame, "columns", [])
    ):
        logger.warning("monitor tape action skipped: CSI 300 close is unavailable")
        return holdings
    try:
        return stamp_holding_trends(holdings, data_map, market_frame["close"])
    except Exception as exc:  # noqa: BLE001 — never fail the scan on tape classify
        logger.warning("monitor holding trend stamp failed: %s", exc)
        return holdings


def reset_csi300_trend_memo() -> None:
    """Drop the in-process CSI 300 tape cache (tests / new calendar day)."""
    global _CSI300_TREND_MEMO
    _CSI300_TREND_MEMO = None


def _csi300_frame_for_trends(
    data_map: dict[str, pd.DataFrame],
    *,
    as_of_date: date,
    lookback_days: int,
    source: str,
    interval: str,
) -> pd.DataFrame | None:
    """Return CSI 300 OHLCV for tape classification.

    Prefers a frame already in ``data_map``, then today's process memo, then a
    T-1 fetch (so ``index_daily`` can hit parquet) plus the same-day overlay.
    """
    global _CSI300_TREND_MEMO
    source_key = str(source or "auto")
    interval_key = str(interval or "1D")
    cached = data_map.get(CSI300)
    if cached is not None and not getattr(cached, "empty", True):
        _CSI300_TREND_MEMO = (as_of_date, source_key, interval_key, cached)
        return cached
    if (
        _CSI300_TREND_MEMO is not None
        and _CSI300_TREND_MEMO[0] == as_of_date
        and _CSI300_TREND_MEMO[1] == source_key
        and _CSI300_TREND_MEMO[2] == interval_key
    ):
        return _CSI300_TREND_MEMO[3]

    overlay = _is_daily_interval(interval)
    # Settled T-1 range is a parquet key; end=today would skip cache and
    # burn the hourly index_daily quota on every strategy switch.
    fetch_end = as_of_date - timedelta(days=1) if overlay else as_of_date
    lookback = max(int(lookback_days), _CSI300_LOOKBACK_DAYS)
    start = (as_of_date - timedelta(days=lookback)).isoformat()
    try:
        extra, errors = _fetch_universe_ohlcv(
            [CSI300],
            start=start,
            end=fetch_end.isoformat(),
            source=source,
            interval=interval,
        )
    except Exception as exc:  # noqa: BLE001 — holdings still display without trend
        logger.warning("monitor CSI 300 fetch failed: %s", exc)
        return None
    market_frame = extra.get(CSI300)
    if market_frame is None or getattr(market_frame, "empty", True):
        logger.warning(
            "monitor CSI 300 unavailable: %s",
            errors.get(CSI300) or "no OHLCV bars returned",
        )
        return None
    if overlay:
        try:
            overlaid, _, _ = _apply_same_day_overlay(
                {CSI300: market_frame}, as_of_date=as_of_date
            )
            market_frame = overlaid.get(CSI300, market_frame)
        except Exception as exc:  # noqa: BLE001 — T-1 index close is still usable
            logger.warning("monitor CSI 300 overlay failed: %s", exc)
    _CSI300_TREND_MEMO = (as_of_date, source_key, interval_key, market_frame)
    return market_frame


def _is_daily_interval(interval: str) -> bool:
    return str(interval or "1D").strip().lower() in {"1d", "d", "day", "daily"}


def _apply_same_day_overlay(
    data_map: dict[str, pd.DataFrame],
    *,
    as_of_date: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, bool], dict[str, Any]]:
    """Patch T-1 frames with a provisional today bar when a live quote exists."""
    from src.monitor.live_bar import ensure_same_day_bar, fetch_live_quotes

    # One batched round-trip path (cache + breaker + Tencent/Sina batches)
    # instead of N serial Eastmoney calls that amplify proxy failures.
    quotes = fetch_live_quotes(list(data_map.keys()))
    patched: dict[str, pd.DataFrame] = {}
    provisional: dict[str, bool] = {}
    quote_ok = 0
    for symbol, frame in data_map.items():
        # fetch_live_quotes keys are upper-cased; data_map keys usually match.
        quote = quotes.get(str(symbol).strip().upper())
        if quote is not None:
            quote_ok += 1
        new_frame, is_provisional = ensure_same_day_bar(frame, quote, as_of_date)
        patched[symbol] = new_frame
        provisional[symbol] = is_provisional
        if not is_provisional and quote is None:
            logger.info(
                "monitor same-day overlay skipped for %s (no live quote; using history through %s)",
                symbol,
                _frame_last_date(frame, as_of_date.isoformat()),
            )

    total = len(data_map)
    coverage = (quote_ok / total) if total else 0.0
    stats: dict[str, Any] = {
        "live_quote_ok": quote_ok,
        "live_quote_total": total,
        "live_quote_coverage": round(coverage, 4),
        "provisional_count": sum(1 for v in provisional.values() if v),
    }
    if total and coverage < 0.5:
        logger.warning(
            "monitor live-quote coverage low: %d/%d (%.0f%%) — same-day signals may lag at T-1",
            quote_ok,
            total,
            coverage * 100.0,
        )
    else:
        logger.info(
            "monitor live-quote coverage: %d/%d (%.0f%%)",
            quote_ok,
            total,
            coverage * 100.0,
        )
    return patched, provisional, stats


def _scan_shadow(
    config: MonitorConfig, as_of_date: date
) -> tuple[list[MonitorSuggestion], list, DayOps, dict[str, Any]]:
    """Map Shadow today-signal hits onto the configured symbol universe."""
    shadow_id = (config.strategy.shadow_id or "").strip()
    if not shadow_id:
        raise MonitorScanError("strategy.shadow_id is required for shadow scans")

    from src.shadow_account.scanner import scan_today_signals
    from src.shadow_account.storage import load_profile

    try:
        profile = load_profile(shadow_id)
    except FileNotFoundError as exc:
        raise MonitorScanError(f"shadow profile not found: {shadow_id!r}") from exc

    wanted = {s.upper() for s in config.symbols}
    try:
        matches = scan_today_signals(profile, target_date=as_of_date, per_market=50)
    except Exception as exc:
        raise MonitorScanError(f"shadow scan failed: {exc}") from exc

    hit_by_symbol = {
        str(m.get("symbol") or "").strip().upper(): m
        for m in matches
        if str(m.get("symbol") or "").strip().upper() in wanted
    }

    as_of_iso = as_of_date.isoformat()
    suggestions: list[MonitorSuggestion] = []
    current_weights: dict[str, float] = {}
    meta_by_symbol: dict[str, dict] = {}
    for symbol in config.symbols:
        hit = hit_by_symbol.get(symbol)
        if hit:
            reason = str(hit.get("reason") or "shadow rule matched")
            suggestions.append(
                MonitorSuggestion(
                    symbol=symbol,
                    action="buy",
                    position="long",
                    reason=reason,
                    as_of=as_of_iso,
                    data_as_of=as_of_iso,
                )
            )
            current_weights[symbol] = 1.0
            meta_by_symbol[symbol] = {"data_as_of": as_of_iso, "provisional": False}
        else:
            suggestions.append(
                MonitorSuggestion(
                    symbol=symbol,
                    action="hold",
                    position="flat",
                    reason="no shadow entry rule matched today",
                    as_of=as_of_iso,
                    data_as_of=as_of_iso,
                )
            )

    run_key = f"shadow_{shadow_id}"
    # Same rule as run_dir: baseline is the last scan before today, never
    # an earlier same-day shadow pass.
    prev_snap = load_previous_snapshot(run_key, as_of_iso) if is_safe_history_key(run_key) else None
    previous_weights = (
        previous_weights_from_holdings(prev_snap.holdings) if prev_snap else None
    )
    holdings, ops = build_holdings_and_ops(
        current_weights,
        previous_weights,
        meta_by_symbol=meta_by_symbol,
    )
    snap_meta = {
        "universe_size": len(config.symbols),
        "fetch_error_count": 0,
        "provisional": False,
    }
    return suggestions, holdings, ops, snap_meta


def _position_from_signal(curr: float) -> str:
    """Map latest signal value to an implied position stance."""
    if curr > 0:
        return "long"
    if curr < 0:
        return "short"
    return "flat"


def _map_series_to_suggestion(
    symbol: str,
    series: Any,
    as_of_iso: str,
    *,
    data_as_of: str | None = None,
    price: float | None = None,
    provisional: bool = False,
) -> MonitorSuggestion:
    """Map latest vs previous numeric signal into buy / sell / hold + position."""
    try:
        clean = pd.Series(series).dropna()
    except Exception:
        return MonitorSuggestion(
            symbol=symbol,
            action="hold",
            position="unknown",
            reason="signal series is not numeric",
            as_of=as_of_iso,
            data_as_of=data_as_of,
            price=price,
            provisional=provisional,
        )

    if clean.empty:
        return MonitorSuggestion(
            symbol=symbol,
            action="hold",
            position="unknown",
            reason="signal series empty after dropna",
            as_of=as_of_iso,
            data_as_of=data_as_of,
            price=price,
            provisional=provisional,
        )

    curr = float(clean.iloc[-1])
    prev = float(clean.iloc[-2]) if len(clean) >= 2 else 0.0
    bar_as_of = data_as_of or _index_as_of(clean.index[-1], as_of_iso)
    position = _position_from_signal(curr)

    if prev <= 0 and curr > 0:
        action = "buy"
        reason = f"signal crossed into long ({prev:.4g} → {curr:.4g})"
    elif prev > 0 and curr <= 0:
        action = "sell"
        reason = f"signal exited long ({prev:.4g} → {curr:.4g})"
    elif curr > 0:
        action = "hold"
        reason = f"stay long (signal {curr:.4g})"
    else:
        action = "hold"
        reason = f"stay flat (signal {curr:.4g})"

    if provisional:
        reason = f"[provisional same-day] {reason}"

    confidence = min(1.0, abs(curr)) if abs(curr) <= 1.0 else None
    return MonitorSuggestion(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        position=position,  # type: ignore[arg-type]
        reason=reason,
        as_of=as_of_iso,
        data_as_of=bar_as_of,
        price=price,
        provisional=provisional,
        confidence=confidence,
        signal=curr,
    )


def _index_as_of(label: Any, fallback: str) -> str:
    try:
        ts = pd.Timestamp(label)
        if pd.isna(ts):
            return fallback
        return ts.date().isoformat()
    except Exception:
        return fallback


def _frame_last_date(frame: pd.DataFrame, fallback: str) -> str:
    """Return the last OHLCV bar date for transparency when feeds lag."""
    if frame is None or getattr(frame, "empty", True):
        return fallback
    try:
        return _index_as_of(frame.index[-1], fallback)
    except Exception:
        return fallback


def _frame_last_close(frame: pd.DataFrame) -> float | None:
    """Return the latest close from an OHLCV frame, or None if missing."""
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        if "close" not in frame.columns:
            return None
        value = frame["close"].dropna().iloc[-1]
        price = float(value)
        if price != price:  # NaN
            return None
        return price
    except Exception:
        return None


def _load_signal_engine(signal_path: Path) -> Any:
    """Load SignalEngine from a run's code file via the backtest validator."""
    from backtest.runner import (
        _load_module_from_file,
        _validate_signal_engine_class,
    )

    module_name = f"monitor_signal_engine_{uuid.uuid4().hex[:10]}"
    module = _load_module_from_file(signal_path, module_name)
    engine_cls = getattr(module, "SignalEngine", None)
    if engine_cls is None:
        raise MonitorScanError("signal_engine.py has no SignalEngine class")
    _validate_signal_engine_class(engine_cls)
    return engine_cls()


def _run_source_interval(run_dir: Path) -> tuple[str, str]:
    """Read source/interval from the run config when present."""
    import json

    config_path = run_dir / "config.json"
    source = "auto"
    interval = "1D"
    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                source = str(raw.get("source") or "auto") or "auto"
                interval = str(raw.get("interval") or "1D") or "1D"
        except (OSError, json.JSONDecodeError):
            pass
    return source, interval


def _fetch_universe_ohlcv(
    symbols: list[str],
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Fetch the full universe in one loader round-trip.

    Per-symbol ``fetch_data_map`` churns Tushare's HTTP session (Windows 10054
    / empty JSON body) and skips the loader's multi-code ``daily()`` batch.
    ``allow_partial`` keeps successfully fetched names when a few tickers miss.
    """
    data_map: dict[str, pd.DataFrame] = {}
    fetch_errors: dict[str, str] = {}
    if not symbols:
        return data_map, fetch_errors

    from backtest.runner import fetch_data_map

    batch_ok = False
    batch_error: str | None = None
    try:
        result = fetch_data_map(
            {
                "source": source or "auto",
                "codes": list(symbols),
                "start_date": start,
                "end_date": end,
                "interval": interval or "1D",
            },
            allow_partial=True,
        )
        batch_ok = True
        fetched = getattr(result, "data_map", None) or {}
        for symbol in symbols:
            frame = fetched.get(symbol)
            if frame is None and len(fetched) == 1 and len(symbols) == 1:
                frame = next(iter(fetched.values()))
            if frame is None or getattr(frame, "empty", True):
                continue
            data_map[symbol] = frame
    except Exception as exc:  # noqa: BLE001 — fail-soft; fill via _fetch_one
        batch_error = str(exc)
        logger.warning("monitor batch OHLCV fetch failed: %s", exc)

    if batch_ok:
        for symbol in symbols:
            if symbol not in data_map:
                fetch_errors[symbol] = "no OHLCV bars returned"
        return data_map, fetch_errors

    # A failed batch of hundreds of names must not degrade into per-symbol
    # Tencent/Eastmoney walks (15s timeouts × N). Small universes still retry.
    if len(symbols) > _LARGE_UNIVERSE:
        msg = batch_error or "batch fetch failed"
        logger.warning(
            "skipping per-symbol OHLCV fallback for %d names after batch failure",
            len(symbols),
        )
        for symbol in symbols:
            fetch_errors[symbol] = msg
        return data_map, fetch_errors

    # Batch raised before returning anything useful — try one-by-one so a
    # single poisoned session does not wipe the whole board.
    for symbol in symbols:
        try:
            frame = _fetch_one(
                symbol, start=start, end=end, source=source, interval=interval
            )
            if frame is None or frame.empty:
                fetch_errors[symbol] = "no OHLCV bars returned"
            else:
                data_map[symbol] = frame
        except Exception as exc:  # noqa: BLE001 — fail-soft per symbol
            logger.warning("monitor OHLCV fetch failed for %s: %s", symbol, exc)
            fetch_errors[symbol] = str(exc)
    return data_map, fetch_errors


def _fetch_one(
    symbol: str,
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
) -> pd.DataFrame | None:
    from backtest.runner import fetch_data_map

    result = fetch_data_map(
        {
            "source": source or "auto",
            "codes": [symbol],
            "start_date": start,
            "end_date": end,
            "interval": interval or "1D",
        }
    )
    data_map = getattr(result, "data_map", None) or {}
    frame = data_map.get(symbol)
    if frame is None:
        if len(data_map) == 1:
            frame = next(iter(data_map.values()))
    return frame


def validate_strategy_ref(strategy: StrategyRef, *, runs_root: Path | None = None) -> None:
    """Raise MonitorScanError when the strategy pointer cannot be resolved."""
    if strategy.type == "run_dir":
        run_id = (strategy.run_id or "").strip()
        if not run_id:
            raise MonitorScanError("strategy.run_id is required")
        path = (runs_root or RUNS_DIR) / run_id / "code" / "signal_engine.py"
        if not path.is_file():
            raise MonitorScanError(f"run {run_id!r} has no code/signal_engine.py")
        return
    if strategy.type == "shadow":
        shadow_id = (strategy.shadow_id or "").strip()
        if not shadow_id:
            raise MonitorScanError("strategy.shadow_id is required")
        from src.shadow_account.storage import profiles_dir

        path = profiles_dir() / f"{shadow_id}.json"
        if not path.is_file():
            raise MonitorScanError(f"shadow profile not found: {shadow_id!r}")
        return
    raise MonitorScanError(f"unsupported strategy type: {strategy.type!r}")
