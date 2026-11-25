"""Next-session actionable trade plan sheet (《次日交易计划卡》) for /monitor.

Translates abstract SignalEngine / ProcessMandate weights into concrete,
risk-defined personal orders:
  - Reconciles the user's real positions first (hard stop-loss check, T+1 lock,
    signal exit, or hold with trailing stop);
  - Sizes new focus entries by risk-per-trade (default 1.5% of total capital),
    capped by user's habitual -10% max stop distance and 2-3 max positions;
  - Emits exact share counts (STAR 200+ / Mainboard 100-lot), ideal buy zone,
    gap-up no-chase line, hard stop price, max RMB loss, and broker conditional
    order instructions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd

from backtest.engines.china_a import ChinaAEngine, min_buy_lot
from src.monitor.conviction import ConvictionItem, ConvictionSummary
from src.monitor.models import MonitorConfig
from src.monitor.my_portfolio import MyPortfolio, load_my_portfolio

PlanAction = Literal[
    "buy_focus",
    "stop_exit",
    "signal_exit",
    "hold_keep",
    "slot_full_watch",
    "skip_small_delta",
]


@dataclass
class PlannedOrder:
    """One concrete next-session action row on the personal Trade Plan Sheet."""

    symbol: str
    action: PlanAction
    title: str
    ref_close: float
    planned_shares: int = 0
    planned_amount: float = 0.0
    capital_weight: float = 0.0
    ideal_buy_low: float | None = None
    ideal_buy_high: float | None = None
    chase_stop_above: float | None = None
    stop_price: float | None = None
    stop_distance_pct: float | None = None
    target_price: float | None = None
    reward_risk_ratio: float | None = None
    max_loss_rmb: float | None = None
    max_loss_capital_pct: float | None = None
    held_shares: int = 0
    sellable_shares: int = 0
    cost_price: float | None = None
    unrealized_pnl_pct: float | None = None
    stars: float | None = None
    board_name: str | None = None
    broker_tip: str = ""
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradePlanSheet:
    """Complete next-session plan combining real portfolio + Top 2-3 focus picks."""

    as_of: str
    exec_session: str
    total_capital: float
    effective_cash: float
    max_positions: int
    held_count: int
    slots_available: int
    max_stop_loss_pct: float
    risk_per_trade_pct: float
    summary_note: str
    position_actions: list[PlannedOrder] = field(default_factory=list)
    entry_plans: list[PlannedOrder] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "exec_session": self.exec_session,
            "total_capital": self.total_capital,
            "effective_cash": self.effective_cash,
            "max_positions": self.max_positions,
            "held_count": self.held_count,
            "slots_available": self.slots_available,
            "max_stop_loss_pct": self.max_stop_loss_pct,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "summary_note": self.summary_note,
            "position_actions": [row.to_dict() for row in self.position_actions],
            "entry_plans": [row.to_dict() for row in self.entry_plans],
        }


def build_trade_plan(
    config: MonitorConfig,
    *,
    as_of: str,
    exec_session: str,
    conviction: ConvictionSummary,
    portfolio: MyPortfolio | None = None,
    data_map: Mapping[str, pd.DataFrame] | None = None,
) -> TradePlanSheet:
    """Generate deterministic next-session trade plan from real portfolio & focus picks."""
    book = portfolio if portfolio is not None else load_my_portfolio()
    engine = ChinaAEngine({})

    price_lookup = _latest_prices(config, data_map)
    cash_left = book.effective_cash(price_lookup)

    conv_by_sym: dict[str, ConvictionItem] = {item.symbol: item for item in conviction.items}
    active_strategy_syms = {h.symbol.strip().upper() for h in config.holdings if h.symbol and h.weight > 1e-9}
    exited_syms = {s.strip().upper() for s in (config.ops.exited or []) if s}
    dec_by_sym = _decision_weights(config)

    position_actions: list[PlannedOrder] = []
    kept_slots = 0

    # 1. Evaluate user's existing real positions first (stop-loss & exit discipline).
    for pos in book.positions:
        sym = pos.symbol
        frame = (data_map or {}).get(sym)
        mark = price_lookup.get(sym) or pos.cost_price
        pnl_pct = (mark - pos.cost_price) / pos.cost_price if pos.cost_price > 0 else 0.0
        t1_locked = bool(pos.buy_date) and pos.buy_date >= as_of
        sellable = 0 if t1_locked else pos.shares

        # Effective stop price: user explicit stop, else max(cost*(1 - max_stop_pct), trailing structure).
        default_cost_stop = round(pos.cost_price * (1.0 - book.max_stop_loss_pct), 2)
        atr_val, swing_low = _atr_and_support(frame, mark)
        trail_stop = round(max(default_cost_stop, min(swing_low, mark - 2.2 * atr_val)), 2)
        eff_stop = pos.stop_price if (pos.stop_price is not None and pos.stop_price > 0) else trail_stop
        stop_dist = (mark - eff_stop) / mark if mark > 0 else 0.0

        conv = conv_by_sym.get(sym)
        warns: list[str] = list(conv.cautions if conv is not None else [])
        if t1_locked:
            warns.append("今日新买仓位受 T+1 限制，最早下一交易日可卖")

        if mark <= eff_stop + 1e-6:
            position_actions.append(
                PlannedOrder(
                    symbol=sym,
                    action="stop_exit",
                    title="触发止损纪律 · 次日卖出",
                    ref_close=round(mark, 3),
                    planned_shares=sellable,
                    planned_amount=round(sellable * mark, 2),
                    stop_price=eff_stop,
                    stop_distance_pct=round(stop_dist, 4),
                    held_shares=pos.shares,
                    sellable_shares=sellable,
                    cost_price=pos.cost_price,
                    unrealized_pnl_pct=round(pnl_pct, 4),
                    stars=conv.stars if conv else None,
                    board_name=conv.board_name if conv else None,
                    broker_tip=(
                        f"次日集合竞价/早盘无条件卖出 {sellable} 股（最新价 ¥{mark:.2f} 已跌破止损线 ¥{eff_stop:.2f}），绝不扛单。"
                        if sellable > 0
                        else f"已跌破止损线 ¥{eff_stop:.2f}，因 T+1 锁仓今日无法动，下一可卖日早盘第一时间执行纪律离场。"
                    ),
                    reason=f"收盘价 ¥{mark:.2f} 触及硬止损位 ¥{eff_stop:.2f}（浮盈亏 {pnl_pct * 100:+.1f}%）",
                    warnings=warns,
                )
            )
            cash_left += sellable * mark * 0.999
            continue

        dec_act = dec_by_sym.get(sym, ("", 0.0))[0]
        if sym in exited_syms or dec_act == "exit" or (
            active_strategy_syms and sym not in active_strategy_syms and conv is None
        ):
            position_actions.append(
                PlannedOrder(
                    symbol=sym,
                    action="signal_exit",
                    title="策略轮动离场 · 次日卖出",
                    ref_close=round(mark, 3),
                    planned_shares=sellable,
                    planned_amount=round(sellable * mark, 2),
                    stop_price=eff_stop,
                    stop_distance_pct=round(stop_dist, 4),
                    held_shares=pos.shares,
                    sellable_shares=sellable,
                    cost_price=pos.cost_price,
                    unrealized_pnl_pct=round(pnl_pct, 4),
                    stars=conv.stars if conv else None,
                    board_name=conv.board_name if conv else None,
                    broker_tip=(
                        f"次日早盘逢高/分批卖出 {sellable} 股，回收资金等待新共振机会。"
                        if sellable > 0
                        else "策略已发离场信号，当前 T+1 锁仓，解锁首日执行卖出。"
                    ),
                    reason=f"策略信号已退出该标的（当前浮盈亏 {pnl_pct * 100:+.1f}%）",
                    warnings=warns,
                )
            )
            cash_left += sellable * mark * 0.999
            continue

        kept_slots += 1
        position_actions.append(
            PlannedOrder(
                symbol=sym,
                action="hold_keep",
                title="趋势健康 · 守止损续持",
                ref_close=round(mark, 3),
                planned_shares=0,
                planned_amount=round(pos.shares * mark, 2),
                capital_weight=round((pos.shares * mark) / book.total_capital, 4) if book.total_capital > 0 else 0.0,
                stop_price=eff_stop,
                stop_distance_pct=round(stop_dist, 4),
                held_shares=pos.shares,
                sellable_shares=sellable,
                cost_price=pos.cost_price,
                unrealized_pnl_pct=round(pnl_pct, 4),
                stars=conv.stars if conv else None,
                board_name=conv.board_name if conv else None,
                broker_tip=f"券商条件单保持「股价跌破 ¥{eff_stop:.2f} 自动止损卖出」，未破位安心持有，少看盘。",
                reason=f"持仓 {pos.shares} 股（浮盈亏 {pnl_pct * 100:+.1f}%），保护止损线 ¥{eff_stop:.2f}",
                warnings=warns,
            )
        )

    slots_left = max(0, book.max_positions - kept_slots)
    held_map = {p.symbol: p for p in book.positions}

    # 2. Plan high-conviction new entries (strictly limited to slots_left & risk budget).
    entry_plans: list[PlannedOrder] = []
    remaining_cash = max(0.0, cash_left)
    slots_remaining = slots_left

    for sym in conviction.focus_symbols:
        if sym in held_map:
            continue
        conv = conv_by_sym.get(sym)
        if conv is None or conv.vetoed:
            continue
        ref = price_lookup.get(sym)
        if ref is None or ref <= 0:
            continue

        frame = (data_map or {}).get(sym)
        atr_val, swing_low = _atr_and_support(frame, ref)
        # Hard stop is bounded between [2.5%, max_stop_loss_pct (default 10%)] below ref_close.
        floor_10pct = ref * (1.0 - book.max_stop_loss_pct)
        ceiling_min_stop = ref * 0.975
        struct_stop = min(swing_low * 0.995, ref - 2.0 * atr_val)
        stop_px = round(float(np.clip(struct_stop, floor_10pct, ceiling_min_stop)), 2)
        risk_per_share = max(ref - stop_px, ref * 0.025)
        stop_pct = risk_per_share / ref

        ideal_low = round(max(stop_px * 1.01, ref * 0.985), 2)
        ideal_high = round(ref * 1.015, 2)
        chase_cap = round(min(ref * 1.038, ref + 0.85 * atr_val), 2)
        target_px = round(ref + 2.0 * risk_per_share, 2)
        rr_ratio = round((target_px - ref) / risk_per_share, 2)

        if slots_remaining <= 0:
            entry_plans.append(
                PlannedOrder(
                    symbol=sym,
                    action="slot_full_watch",
                    title=f"持仓已满 {book.max_positions} 只 · 仅作备选",
                    ref_close=round(ref, 3),
                    ideal_buy_low=ideal_low,
                    ideal_buy_high=ideal_high,
                    chase_stop_above=chase_cap,
                    stop_price=stop_px,
                    stop_distance_pct=round(stop_pct, 4),
                    target_price=target_px,
                    reward_risk_ratio=rr_ratio,
                    stars=conv.stars,
                    board_name=conv.board_name,
                    broker_tip=f"实盘已满 {book.max_positions} 只上限，除非旧仓止损/止盈腾出槽位，否则不加新票。",
                    reason="；".join(conv.evidence[:2]) or "五维共振达标",
                    warnings=list(conv.cautions),
                )
            )
            continue

        # Risk-based sizing: max loss <= total_capital * risk_per_trade_pct (e.g. 1.5%).
        risk_budget_rmb = book.total_capital * book.risk_per_trade_pct
        shares_by_risk = risk_budget_rmb / risk_per_share

        # Slot & ProcessMandate weight cap.
        slot_cap_weight = min(0.45, 1.0 / max(1, book.max_positions))
        dec_w = dec_by_sym.get(sym, ("", 0.0))[1]
        if dec_w > 1e-9:
            # Scale strategy weight reasonably for concentrated 2-3 stock personal book.
            slot_cap_weight = min(slot_cap_weight, max(dec_w, min(0.35, slot_cap_weight)))
        shares_by_cap = (book.total_capital * slot_cap_weight) / ref
        shares_by_cash = remaining_cash / (ref * 1.002)

        raw_shares = min(shares_by_risk, shares_by_cap, shares_by_cash)
        shares = int(engine.round_size(raw_shares, ref, symbol=sym))
        amount = round(shares * ref, 2)
        warns = list(conv.cautions)

        min_lot = min_buy_lot(sym)
        if shares < min_lot:
            need_rmb = min_lot * ref
            entry_plans.append(
                PlannedOrder(
                    symbol=sym,
                    action="skip_small_delta",
                    title="资金/整手受限 · 暂无法开仓",
                    ref_close=round(ref, 3),
                    ideal_buy_low=ideal_low,
                    ideal_buy_high=ideal_high,
                    chase_stop_above=chase_cap,
                    stop_price=stop_px,
                    stop_distance_pct=round(stop_pct, 4),
                    target_price=target_px,
                    reward_risk_ratio=rr_ratio,
                    stars=conv.stars,
                    board_name=conv.board_name,
                    broker_tip=f"最少需买 {min_lot} 股（约 ¥{need_rmb:,.0f}），当前可用现金或以损定仓额度不足。",
                    reason=f"可用现金 ¥{remaining_cash:,.0f} / 单笔风险预算 ¥{risk_budget_rmb:,.0f}",
                    warnings=warns,
                )
            )
            continue

        if amount < book.min_trade_amount:
            warns.append(f"预计成交额 ¥{amount:,.0f} 低于最小单笔阈值 ¥{book.min_trade_amount:,.0f}（注意 ¥5 低佣磨损）")
        elif amount < 20_000.0:
            warns.append("单笔金额不足 ¥20,000，券商最低 ¥5 佣金会略微抬高摩擦费率")

        max_loss = round(shares * risk_per_share, 2)
        max_loss_pct = round(max_loss / book.total_capital, 4) if book.total_capital > 0 else 0.0
        cap_w = round(amount / book.total_capital, 4) if book.total_capital > 0 else 0.0

        entry_plans.append(
            PlannedOrder(
                symbol=sym,
                action="buy_focus",
                title=f"共振焦点买入 · {shares} 股",
                ref_close=round(ref, 3),
                planned_shares=shares,
                planned_amount=amount,
                capital_weight=cap_w,
                ideal_buy_low=ideal_low,
                ideal_buy_high=ideal_high,
                chase_stop_above=chase_cap,
                stop_price=stop_px,
                stop_distance_pct=round(stop_pct, 4),
                target_price=target_px,
                reward_risk_ratio=rr_ratio,
                max_loss_rmb=max_loss,
                max_loss_capital_pct=max_loss_pct,
                stars=conv.stars,
                board_name=conv.board_name,
                broker_tip=(
                    f"【{exec_session} 盘前计划】理想挂单区间 ¥{ideal_low:.2f}~¥{ideal_high:.2f} 买入 {shares} 股；"
                    f"若集合竞价/早盘高开超过 ¥{chase_cap:.2f} 放弃不追；"
                    f"成交后预设止损条件单 ¥{stop_px:.2f}（最坏亏损 ¥{max_loss:,.0f}，仅占总本金 {max_loss_pct * 100:.2f}%）。"
                ),
                reason="；".join(conv.evidence[:3]) or "五维共振高胜率信号",
                warnings=warns,
            )
        )
        remaining_cash = max(0.0, remaining_cash - amount * 1.0015)
        slots_remaining -= 1

    buy_n = sum(1 for row in entry_plans if row.action == "buy_focus")
    exit_n = sum(1 for row in position_actions if row.action in ("stop_exit", "signal_exit"))
    if exit_n and buy_n:
        note = f"次日（{exec_session}）计划：先处理 {exit_n} 只持仓卖出纪律，再关注 {buy_n} 只共振焦点低吸机会。"
    elif exit_n:
        note = f"次日（{exec_session}）计划：优先执行 {exit_n} 只持仓离场/止损纪律，暂无新开仓。"
    elif buy_n:
        note = f"次日（{exec_session}）计划：聚焦 {buy_n} 只高胜率共振标的，严格按限价带与止损额定仓执行。"
    elif kept_slots > 0:
        note = f"次日（{exec_session}）计划：现有 {kept_slots} 只持仓守住止损线续持，无需频繁折腾调仓。"
    else:
        note = f"次日（{exec_session}）计划：空仓等待，无高确定性买点就是最好的防守。"

    return TradePlanSheet(
        as_of=as_of,
        exec_session=exec_session,
        total_capital=round(book.total_capital, 2),
        effective_cash=round(book.effective_cash(price_lookup), 2),
        max_positions=book.max_positions,
        held_count=len(book.positions),
        slots_available=slots_left,
        max_stop_loss_pct=book.max_stop_loss_pct,
        risk_per_trade_pct=book.risk_per_trade_pct,
        summary_note=note,
        position_actions=position_actions,
        entry_plans=entry_plans,
    )


def _latest_prices(
    config: MonitorConfig,
    data_map: Mapping[str, pd.DataFrame] | None,
) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in config.holdings:
        if row.symbol and row.price and row.price > 0:
            out[row.symbol.strip().upper()] = float(row.price)
    for item in config.suggestions:
        if item.symbol and item.price and item.price > 0:
            out.setdefault(item.symbol.strip().upper(), float(item.price))
    if data_map:
        for sym, frame in data_map.items():
            if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
                continue
            s = pd.to_numeric(frame["close"], errors="coerce").dropna()
            if not s.empty and float(s.iloc[-1]) > 0:
                out[str(sym).strip().upper()] = float(s.iloc[-1])
    return out


def _decision_weights(config: MonitorConfig) -> dict[str, tuple[str, float]]:
    if config.decision is None:
        return {}
    out: dict[str, tuple[str, float]] = {}
    for row in config.decision.per_symbol:
        out[row.symbol.strip().upper()] = (str(row.action), float(row.weight))
    return out


def _atr_and_support(
    frame: pd.DataFrame | None,
    ref_close: float,
) -> tuple[float, float]:
    """Return ``(atr14, recent_10d_low)`` with sensible fallbacks."""
    default_atr = ref_close * 0.035
    default_low = ref_close * 0.94
    if frame is None or getattr(frame, "empty", True):
        return default_atr, default_low
    for col in ("high", "low", "close"):
        if col not in frame.columns:
            return default_atr, default_low
    sub = frame.tail(25)
    highs = pd.to_numeric(sub["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(sub["low"], errors="coerce").to_numpy(dtype=float)
    closes = pd.to_numeric(sub["close"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(highs) & np.isfinite(lows) & np.isfinite(closes) & (lows > 0)
    if int(np.sum(valid)) < 5:
        return default_atr, default_low
    h = highs[valid]
    l = lows[valid]
    c = closes[valid]
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
    recent_low = float(np.min(l[-10:]))
    if not np.isfinite(atr) or atr <= 0:
        atr = default_atr
    if not np.isfinite(recent_low) or recent_low <= 0:
        recent_low = default_low
    return atr, recent_low
