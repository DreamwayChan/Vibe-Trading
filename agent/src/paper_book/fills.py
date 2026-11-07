"""One-order fill using ``ChinaAEngine`` cost and market rules.

Sell checks use direction ``0`` (close). Slippage uses ``+1`` on a buy and
``-1`` on a sell, matching ``BaseEngine._rebalance`` / ``_plan_open_order``.
The reference price is the bar open when it is present, otherwise the close.
"""

from __future__ import annotations

from backtest.engines.china_a import (
    ChinaAEngine,
    _bar_date,
    _is_st_row,
    _bar_suspended,
    _calc_pct_change,
    _panel_value,
    _price_limit,
    _row_suspended,
)
from backtest.models import Position

from src.paper_book.models import Fill, Unfilled


def make_engine(config: dict | None = None) -> ChinaAEngine:
    """Engine whose omitted keys come from ``backtest.ashare_costs``."""
    merged = dict(config or {})
    panel = _tradability_panel()
    if panel is not None and "tradability_index" not in merged:
        merged["tradability_index"] = panel
    return ChinaAEngine(merged)


def _tradability_panel():
    """Attach the lake panel when it has rows. An empty panel leaves the heuristic."""
    try:
        from src.config.accessor import get_env_config

        if not get_env_config().data.vibe_trading_tradability_panel:
            return None
        from src.data.tradability import TradabilityIndex, load_panel

        frame = load_panel()
        if frame is None or frame.empty:
            return None
        return TradabilityIndex(frame)
    except Exception:  # noqa: BLE001 — heuristic fills still run
        return None


def explain_block(
    engine: ChinaAEngine,
    symbol: str,
    direction: int,
    bar,
    entry_time,
) -> str:
    """Why ``can_execute`` refused this order. Labels follow the engine's checks."""
    if engine.t_plus_one and direction == 0 and entry_time is not None:
        bar_day = _bar_date(bar)
        entry_day = entry_time.date() if hasattr(entry_time, "date") else None
        if bar_day is not None and entry_day == bar_day:
            return "t_plus_one"

    panel = engine._tradability_row(symbol, bar)
    if direction == 1 and engine.block_st_buy and _is_st_row(bar, panel):
        return "st"
    if panel is not None and engine.block_suspension and _row_suspended(panel):
        return "suspended"
    if panel is None and engine.block_suspension and _bar_suspended(bar):
        return "suspended"

    if panel is not None and not bool(panel.get("band_mismatch")):
        close = _float_or_none(bar.get("close") if hasattr(bar, "get") else None)
        if close is not None:
            limit_up = _panel_value(panel, "limit_up")
            limit_down = _panel_value(panel, "limit_down")
            if direction == 1 and limit_up is not None and close >= limit_up - 0.005:
                return "limit_up"
            if direction == 0 and limit_down is not None and close <= limit_down + 0.005:
                return "limit_down"

    pct = _calc_pct_change(bar)
    if pct is not None:
        limit = _price_limit(symbol)
        if direction == 1 and pct >= limit - 0.001:
            return "limit_up"
        if direction == 0 and pct <= -limit + 0.001:
            return "limit_down"
    return "unfillable"


def ref_price(bar) -> float | None:
    """Open when the bar has one, otherwise close. Non-positive prices are unusable."""
    for key in ("open", "close"):
        if hasattr(bar, "index") and key not in bar.index and not hasattr(bar, key):
            continue
        price = _float_or_none(bar.get(key) if hasattr(bar, "get") else None)
        if price is not None and price > 0:
            return price
    return None


def mark_price(bar) -> float | None:
    close = _float_or_none(bar.get("close") if hasattr(bar, "get") else None)
    if close is not None and close > 0:
        return close
    return None


def affordable_buy(engine: ChinaAEngine, cash: float, ref: float, desired: float) -> tuple[int, float, float]:
    """Largest 100-share lot whose slipped notional plus commission fits in ``cash``."""
    fill = float(engine.apply_slippage(ref, 1))
    if fill <= 0:
        return 0, fill, 0.0
    shares = int(engine.round_size(desired, fill))
    while shares >= 100:
        commission = float(engine.calc_commission(shares, fill, 1, is_open=True))
        if shares * fill + commission <= cash + 1e-6:
            return shares, fill, commission
        shares -= 100
    return 0, fill, 0.0


def try_buy(
    engine: ChinaAEngine,
    *,
    symbol: str,
    bar,
    cash: float,
    target_shares: int,
    held_shares: int,
) -> tuple[Fill | None, Unfilled | None, float]:
    """Buy the gap to ``target_shares``. Returns the fill, an unfilled row, and cash left."""
    delta = int(target_shares) - int(held_shares)
    if delta < 100:
        return None, None, cash
    close = mark_price(bar)
    ref = ref_price(bar)
    if close is None or ref is None:
        return None, Unfilled(symbol, "buy", delta, "bad_price"), cash
    if not engine.can_execute(symbol, 1, bar):
        reason = explain_block(engine, symbol, 1, bar, None)
        return None, Unfilled(symbol, "buy", delta, reason), cash
    shares, fill, commission = affordable_buy(engine, cash, ref, delta)
    if shares < 100:
        reason = "zero_size" if delta < 100 else "insufficient_cash"
        return None, Unfilled(symbol, "buy", delta, reason), cash
    notional = shares * fill
    cash_after = cash - notional - commission
    adverse = (fill - ref) / ref
    return (
        Fill(
            symbol=symbol,
            side="buy",
            shares=shares,
            close=close,
            ref_price=ref,
            fill_price=fill,
            commission=commission,
            notional=notional,
            slippage=adverse,
        ),
        None,
        cash_after,
    )


def try_sell(
    engine: ChinaAEngine,
    *,
    symbol: str,
    bar,
    sellable: int,
    locked: int,
    entry_time,
) -> tuple[Fill | None, list[Unfilled]]:
    """Sell shares whose lots are already past T+1. Locked lots stay put."""
    unfilled: list[Unfilled] = []
    total = int(sellable) + int(locked)
    if total <= 0:
        return None, unfilled
    close = mark_price(bar)
    ref = ref_price(bar)
    if close is None or ref is None:
        return None, [Unfilled(symbol, "sell", total, "bad_price")]
    if sellable <= 0:
        return None, [Unfilled(symbol, "sell", locked, "t_plus_one")]

    engine.positions[symbol] = Position(
        symbol=symbol,
        direction=1,
        entry_price=ref,
        entry_time=entry_time,
        size=float(sellable),
    )
    try:
        allowed = engine.can_execute(symbol, 0, bar)
        reason = None if allowed else explain_block(engine, symbol, 0, bar, entry_time)
    finally:
        engine.positions.pop(symbol, None)

    if not allowed:
        return None, [Unfilled(symbol, "sell", total, reason or "unfillable")]

    fill_price = float(engine.apply_slippage(ref, -1))
    commission = float(engine.calc_commission(sellable, fill_price, 0, is_open=False))
    notional = sellable * fill_price
    adverse = (ref - fill_price) / ref if ref else 0.0
    filled = Fill(
        symbol=symbol,
        side="sell",
        shares=int(sellable),
        close=close,
        ref_price=ref,
        fill_price=fill_price,
        commission=commission,
        notional=notional,
        slippage=adverse,
    )
    if locked > 0:
        unfilled.append(Unfilled(symbol, "sell", int(locked), "t_plus_one"))
    return filled, unfilled


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number
