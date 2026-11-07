"""Step one A-share session and persist the book."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from backtest.engines.china_a import ChinaAEngine

from src.monitor.models import MonitorConfig
from src.paper_book.caps import session_caps
from src.paper_book.downgrade import downgrade_admission
from src.paper_book.expectation import (
    choose_anchor,
    expectation_metrics,
    load_equity,
    slippage_gap,
)
from src.paper_book.fills import make_engine, mark_price, ref_price, try_buy, try_sell
from src.paper_book.models import (
    PAPER_BOOK_DEVIATION,
    BookState,
    DailyReport,
    Fill,
    Intent,
    Lot,
    PositionState,
    Unfilled,
)
from src.paper_book.signals import resolve_intents
from src.paper_book.store import (
    PaperBookError,
    load_report,
    load_state,
    paper_book_root,
    previous_nav,
    require_book_id,
    require_date,
    save_report,
    save_state,
)

logger = logging.getLogger(__name__)

__all__ = ["PaperBookError", "load_day_bars", "resolve_run_dir", "step_day"]

_AUTO_STORE = object()


def step_day(
    *,
    as_of: str,
    config: MonitorConfig,
    book_id: str | None = None,
    run_dir: Path | None = None,
    root: Path | str | None = None,
    bars: Mapping[str, pd.Series] | None = None,
    strategy_store: Any = _AUTO_STORE,
    threshold: float | None = None,
    initial_cash: float | None = None,
    max_position: float | None = None,
    max_drawdown: float | None = None,
    engine: ChinaAEngine | None = None,
) -> DailyReport:
    """Settle ``as_of`` from tradable signals, lake (or injected) bars, and the saved book.

    Restepping the latest session replays that session's saved intents from
    the opening checkpoint, so a downgrade written at the end of the session
    does not erase the fills. An earlier session is rejected.
    """
    day = require_date(as_of)
    resolved_id = require_book_id(book_id or _default_book_id(config))
    store_root = paper_book_root(root)
    run_id = (config.strategy.run_id or "").strip()
    limit = _threshold(threshold)
    cash0 = _initial_cash(initial_cash)

    state = load_state(store_root, resolved_id)
    replay_intents: list[Intent] | None = None
    skipped: list[dict[str, str]] = []
    prior_risk: dict[str, Any] = {}
    fresh_session = True
    if state is None:
        state = BookState(
            book_id=resolved_id,
            run_id=run_id,
            initial_cash=cash0,
            cash=cash0,
        )
    else:
        if state.settled_through and day < state.settled_through:
            raise PaperBookError(
                f"session {day} is before settled date {state.settled_through}"
            )
        if state.settled_through == day:
            if not isinstance(state.opening, dict):
                raise PaperBookError(f"session {day} has no opening checkpoint")
            prior = load_report(store_root, resolved_id, day) or {}
            replay_intents = [
                Intent.from_dict(item)
                for item in (prior.get("intents") or [])
                if isinstance(item, dict)
            ]
            skipped = [item for item in (prior.get("skipped") or []) if isinstance(item, dict)]
            stored_risk = prior.get("risk")
            prior_risk = stored_risk if isinstance(stored_risk, dict) else {}
            state = BookState.from_dict(state.opening)
            state.book_id = resolved_id
            fresh_session = False

    if state.inception_date is None:
        state.inception_date = day
    if run_id and not state.run_id:
        state.run_id = run_id

    if replay_intents is None:
        replay_intents, skipped = resolve_intents(config, day)

    opening = _checkpoint(state)
    fill_engine = engine if engine is not None else make_engine()
    symbols = _symbols(state, replay_intents)
    day_bars = dict(bars) if bars is not None else load_day_bars(symbols, day)
    day_bars = {_norm_symbol(key): value for key, value in day_bars.items()}

    max_position = _fraction("max position", _position_cap(max_position))
    max_drawdown = _fraction("max drawdown", _drawdown_cap(max_drawdown))
    fills, unfilled, cap_skipped, risk, replay_intents = _apply_intents(
        state,
        day=day,
        intents=replay_intents,
        bars=day_bars,
        engine=fill_engine,
        max_position=max_position,
        max_drawdown=max_drawdown,
        apply_caps=fresh_session,
        prior_risk=prior_risk,
    )
    if fresh_session:
        skipped.extend(cap_skipped)
    _mark_positions(state, day_bars)

    nav, market_value = _nav(state)
    state.peak_nav = max(state.peak_nav or 0.0, state.initial_cash, nav)
    prev = previous_nav(store_root, resolved_id, day, state.initial_cash)
    daily_return = (nav / prev - 1.0) if prev > 0 else 0.0
    cum_return = (nav / state.initial_cash - 1.0) if state.initial_cash > 0 else 0.0
    # Same convention as backtest.metrics.calc_trade_turnover_series: gross
    # traded value over twice the equity, so a full entry is 0.5 turnover.
    traded = sum(fill.notional for fill in fills)
    turnover = traded / (2.0 * prev) if prev > 0 else 0.0

    equity = load_equity(run_dir)
    if state.anchor_equity is None and equity is not None:
        chosen = choose_anchor(equity, state.inception_date or day)
        if chosen is not None:
            state.anchor_date, state.anchor_equity = chosen
    expectation = expectation_metrics(
        equity,
        as_of=day,
        anchor_date=state.anchor_date,
        anchor_equity=state.anchor_equity,
        initial_cash=state.initial_cash,
        paper_cum_return=cum_return,
        paper_daily_return=daily_return,
    )
    gap = slippage_gap(
        run_dir,
        day,
        [fill.to_dict() for fill in fills],
    )
    expectation["slippage_gap"] = gap
    expectation["threshold"] = limit
    deviation = expectation.get("return_deviation")
    exceeded = deviation is not None and float(deviation) > limit
    expectation["exceeded"] = exceeded

    state.opening = opening
    state.settled_through = day

    downgraded = False
    if exceeded:
        store = strategy_store
        if store is _AUTO_STORE:
            store = _open_store()
        downgrade_admission(
            config,
            run_key=run_id or resolved_id,
            run_dir=str(run_dir) if run_dir is not None else None,
            deviation=float(deviation),
            threshold=limit,
            as_of=day,
            strategy_store=None if store is _AUTO_STORE else store,
        )
        downgraded = True

    report = DailyReport(
        book_id=resolved_id,
        run_id=state.run_id,
        as_of=day,
        nav=nav,
        cash=state.cash,
        market_value=market_value,
        initial_cash=state.initial_cash,
        prev_nav=prev,
        daily_return=daily_return,
        cum_return=cum_return,
        turnover=turnover,
        fills=fills,
        unfilled=unfilled,
        positions=_position_rows(state),
        intents=[intent.to_dict() for intent in replay_intents],
        skipped=skipped,
        expectation=expectation,
        downgraded=downgraded,
        downgrade_reason=PAPER_BOOK_DEVIATION if downgraded else None,
        risk=risk,
    )
    save_state(store_root, state)
    save_report(store_root, report)
    logger.info(
        "paper book %s settled %s nav=%.2f fills=%d unfilled=%d downgraded=%s",
        resolved_id,
        day,
        nav,
        len(fills),
        len(unfilled),
        downgraded,
    )
    return report


def resolve_run_dir(run_id: str) -> Path | None:
    """Bound backtest directory for a monitor run id, or None when it is unsafe or missing."""
    from src.monitor.store import is_safe_history_key

    text = (run_id or "").strip()
    if not is_safe_history_key(text):
        return None
    from src.api.helpers import RUNS_DIR

    root = RUNS_DIR.resolve()
    path = (root / text).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_dir() else None


def load_day_bars(symbols: list[str], as_of: str) -> dict[str, pd.Series]:
    """Settled lake bars for ``as_of``. Never calls the network.

    ``pre_close`` is the previous stored close when the column itself is absent,
    so the limit heuristic has the same input the engine uses in a backtest.
    """
    if not symbols:
        return {}
    start = (pd.Timestamp(as_of) - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    try:
        from src.data.lake_fetch import read_lake

        stored = read_lake(list(symbols), start=start, end=as_of, interval="1d")
    except Exception as exc:  # noqa: BLE001 — a missing lake is an empty book day
        logger.warning("paper book lake read failed: %s", exc)
        return {}
    frames = getattr(stored, "frames", None) or {}
    out: dict[str, pd.Series] = {}
    for symbol, frame in frames.items():
        series = _bar_on(frame, as_of)
        if series is not None:
            out[_norm_symbol(symbol)] = series
    return out


def _apply_intents(
    state: BookState,
    *,
    day: str,
    intents: list[Intent],
    bars: Mapping[str, pd.Series],
    engine: ChinaAEngine,
    max_position: float,
    max_drawdown: float,
    apply_caps: bool,
    prior_risk: dict[str, Any] | None = None,
) -> tuple[list[Fill], list[Unfilled], list[dict[str, str]], dict[str, Any], list[Intent]]:
    fills: list[Fill] = []
    unfilled: list[Unfilled] = []
    sells = [intent for intent in intents if intent.action == "sell"]
    buys = [intent for intent in intents if intent.action == "buy"]

    for intent in sells:
        fill, rows = _sell_intent(state, day=day, intent=intent, bars=bars, engine=engine)
        if fill is not None:
            fills.append(fill)
        unfilled.extend(rows)
    _mark_positions(state, bars)
    nav, _market = _nav(state)
    cap_skipped: list[dict[str, str]] = []
    if apply_caps:
        buys, cap_skipped, risk = session_caps(
            state,
            buys,
            max_position=max_position,
            max_drawdown=max_drawdown,
            nav=nav,
        )
    else:
        # Replay keeps the saved buys. Recompute the mark so the report still
        # shows today's drawdown, and keep the clip list from the first pass.
        peak = state.peak_nav if state.peak_nav and state.peak_nav > 0 else state.initial_cash
        drawdown = ((peak - nav) / peak) if peak > 0 else 0.0
        stored = prior_risk or {}
        clipped = [str(item) for item in (stored.get("clipped") or [])]
        if "drawdown_blocked" in stored:
            blocked = bool(stored.get("drawdown_blocked"))
        else:
            blocked = drawdown > max_drawdown
        risk = {
            "max_position": max_position,
            "max_drawdown": max_drawdown,
            "peak_nav": peak,
            "nav_before_buys": nav,
            "drawdown": drawdown,
            "drawdown_blocked": blocked,
            "clipped": clipped,
        }
    for intent in buys:
        fill, row = _buy_intent(state, day=day, intent=intent, bars=bars, engine=engine, nav=nav)
        if fill is not None:
            fills.append(fill)
        if row is not None:
            unfilled.append(row)
    return fills, unfilled, cap_skipped, risk, sells + buys


def _sell_intent(
    state: BookState,
    *,
    day: str,
    intent: Intent,
    bars: Mapping[str, pd.Series],
    engine: ChinaAEngine,
) -> tuple[Fill | None, list[Unfilled]]:
    position = state.positions.get(intent.symbol)
    if position is None or position.shares <= 0:
        return None, []
    bar = bars.get(intent.symbol)
    if bar is None:
        return None, [Unfilled(intent.symbol, "sell", position.shares, "missing_bar")]
    bar = _session_bar(bar, day)
    sellable, locked, entry = _split_lots(position, day, t_plus_one=engine.t_plus_one)
    entry_time = pd.Timestamp(entry) if entry else pd.Timestamp(day)
    fill, rows = try_sell(
        engine,
        symbol=intent.symbol,
        bar=bar,
        sellable=sellable,
        locked=locked,
        entry_time=entry_time,
    )
    if fill is not None:
        state.cash += fill.notional - fill.commission
        _consume_lots(position, fill.shares, day, t_plus_one=engine.t_plus_one)
        position.last_price = fill.close
        if position.shares <= 0:
            state.positions.pop(intent.symbol, None)
    return fill, rows


def _buy_intent(
    state: BookState,
    *,
    day: str,
    intent: Intent,
    bars: Mapping[str, pd.Series],
    engine: ChinaAEngine,
    nav: float,
) -> tuple[Fill | None, Unfilled | None]:
    if intent.target_weight <= 0:
        return None, None
    bar = bars.get(intent.symbol)
    if bar is None:
        return None, Unfilled(intent.symbol, "buy", 0, "missing_bar")
    bar = _session_bar(bar, day)
    ref = ref_price(bar)
    if ref is None or nav <= 0:
        return None, Unfilled(intent.symbol, "buy", 0, "bad_price")
    slipped = float(engine.apply_slippage(ref, 1))
    target_value = intent.target_weight * nav
    target_shares = int(engine.round_size(target_value / slipped, slipped)) if slipped > 0 else 0
    held = state.positions.get(intent.symbol)
    held_shares = held.shares if held is not None else 0
    fill, row, cash_after = try_buy(
        engine,
        symbol=intent.symbol,
        bar=bar,
        cash=state.cash,
        target_shares=target_shares,
        held_shares=held_shares,
    )
    if fill is None:
        return None, row
    state.cash = cash_after
    position = state.positions.get(intent.symbol)
    if position is None:
        position = PositionState(symbol=intent.symbol)
        state.positions[intent.symbol] = position
    position.lots.append(Lot(shares=fill.shares, buy_date=day, price=fill.fill_price))
    position.last_price = fill.close
    return fill, None


def _split_lots(
    position: PositionState,
    day: str,
    *,
    t_plus_one: bool,
) -> tuple[int, int, str | None]:
    """Split lots into shares the session may sell and shares T+1 still locks.

    With T+1 off, every lot is sellable. ``entry`` is the earliest sellable
    buy date so ``ChinaAEngine.can_execute`` sees the same clock.
    """
    sellable = 0
    locked = 0
    entry: str | None = None
    for lot in position.lots:
        aged = bool(lot.buy_date) and lot.buy_date < day
        if aged or not t_plus_one:
            sellable += lot.shares
            if lot.buy_date and (entry is None or lot.buy_date < entry):
                entry = lot.buy_date
        else:
            locked += lot.shares
    return sellable, locked, entry


def _consume_lots(position: PositionState, shares: int, day: str, *, t_plus_one: bool) -> None:
    remaining = shares
    kept: list[Lot] = []
    for lot in sorted(position.lots, key=lambda item: item.buy_date):
        eligible = (not t_plus_one) or (lot.buy_date < day)
        if remaining > 0 and eligible:
            take = min(lot.shares, remaining)
            lot.shares -= take
            remaining -= take
        if lot.shares > 0:
            kept.append(lot)
    position.lots = kept


def _mark_positions(state: BookState, bars: Mapping[str, pd.Series]) -> None:
    for symbol, position in state.positions.items():
        bar = bars.get(symbol)
        if bar is None:
            continue
        price = mark_price(bar)
        if price is not None:
            position.last_price = price


def _nav(state: BookState) -> tuple[float, float]:
    market = 0.0
    for position in state.positions.values():
        market += position.shares * position.last_price
    return state.cash + market, market


def _position_rows(state: BookState) -> list[dict[str, Any]]:
    rows = []
    for symbol in sorted(state.positions):
        position = state.positions[symbol]
        rows.append(
            {
                "symbol": symbol,
                "shares": position.shares,
                "last_price": position.last_price,
                "market_value": position.shares * position.last_price,
            }
        )
    return rows


def _symbols(state: BookState, intents: list[Intent]) -> list[str]:
    names = set(state.positions)
    names.update(intent.symbol for intent in intents)
    return sorted(names)


def _checkpoint(state: BookState) -> dict[str, Any]:
    payload = state.to_dict()
    payload["opening"] = None
    payload["settled_through"] = state.settled_through
    return payload


def _default_book_id(config: MonitorConfig) -> str:
    run_id = (config.strategy.run_id or "").strip()
    if run_id:
        return run_id
    shadow_id = (config.strategy.shadow_id or "").strip()
    if shadow_id:
        return f"shadow_{shadow_id}"
    return "default"


def _threshold(explicit: float | None) -> float:
    if explicit is not None:
        value = float(explicit)
    else:
        from src.config.accessor import get_env_config

        value = float(get_env_config().data.vibe_trading_paper_book_deviation_threshold)
    if value < 0 or value != value:
        raise PaperBookError(f"deviation threshold must be >= 0, got {value}")
    return value


def _position_cap(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    from src.config.accessor import get_env_config

    return float(get_env_config().data.vibe_trading_paper_book_max_position)


def _drawdown_cap(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    from src.config.accessor import get_env_config

    return float(get_env_config().data.vibe_trading_paper_book_max_drawdown)


def _fraction(label: str, value: float) -> float:
    if value <= 0 or value > 1 or value != value:
        raise PaperBookError(f"{label} must be in (0, 1], got {value}")
    return value


def _initial_cash(explicit: float | None) -> float:
    if explicit is not None:
        value = float(explicit)
    else:
        from src.config.accessor import get_env_config

        value = float(get_env_config().data.vibe_trading_paper_book_initial_cash)
    if value <= 0:
        raise PaperBookError(f"initial cash must be positive, got {value}")
    return value


def _open_store():
    try:
        from src.monitor.admission import open_strategy_store

        return open_strategy_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper book could not open the strategy store: %s", exc)
        return None


def _norm_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _session_bar(bar: pd.Series, day: str) -> pd.Series:
    """Stamp the session date so T+1 compares lots against this bar."""
    if "trade_date" in getattr(bar, "index", []) and _present(bar.get("trade_date")):
        return bar
    data = {str(key): bar[key] for key in bar.index}
    data["trade_date"] = pd.Timestamp(day)
    return pd.Series(data)


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        return True
    return True


def _bar_on(frame: pd.DataFrame, as_of: str) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    index = pd.to_datetime(frame.index, errors="coerce")
    day = pd.Timestamp(as_of).normalize()
    normalized = index.normalize()
    mask = normalized == day
    if not bool(mask.any()):
        return None
    row = frame.loc[mask].iloc[-1]
    data = {str(key): row[key] for key in row.index}
    data["trade_date"] = day
    pre_close = data.get("pre_close")
    pre_ok = False
    try:
        pre_ok = pre_close is not None and float(pre_close) > 0 and float(pre_close) == float(pre_close)
    except (TypeError, ValueError):
        pre_ok = False
    if not pre_ok:
        earlier = frame.loc[normalized < day]
        if not earlier.empty and "close" in earlier.columns:
            data["pre_close"] = float(earlier["close"].iloc[-1])
    return pd.Series(data)
