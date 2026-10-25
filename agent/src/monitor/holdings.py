"""Derive strategy holdings and day-over-day ops from weight maps."""

from __future__ import annotations

from typing import Iterable

from src.monitor.models import (
    WEIGHT_EPSILON,
    DayOps,
    HoldingAction,
    HoldingRow,
)


def weights_from_signal_map(
    signal_map: dict,
    *,
    epsilon: float = WEIGHT_EPSILON,
) -> dict[str, float]:
    """Take the latest bar of each series; keep abs(weight) > epsilon."""
    out: dict[str, float] = {}
    for raw_symbol, series in (signal_map or {}).items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        try:
            import pandas as pd

            clean = pd.Series(series).dropna()
            if clean.empty:
                continue
            weight = float(clean.iloc[-1])
        except Exception:
            continue
        if abs(weight) > epsilon:
            out[symbol] = weight
    return out


def build_holdings_and_ops(
    current_weights: dict[str, float],
    previous_weights: dict[str, float] | None,
    *,
    meta_by_symbol: dict[str, dict] | None = None,
    epsilon: float = WEIGHT_EPSILON,
) -> tuple[list[HoldingRow], DayOps]:
    """Diff current vs previous weight maps into holdings rows + day ops.

    First scan (no previous snapshot): every current name is ``enter``.
    Same-day rescans must pass the *prior day's* holdings scan as
    ``previous_weights`` — today's intraday results do not count as the
    baseline, so each rescan is a fresh realtime enter/exit recalculation.
    """
    prev = previous_weights or {}
    meta = meta_by_symbol or {}

    entered: list[str] = []
    exited: list[str] = []
    held: list[str] = []
    rows: list[HoldingRow] = []

    all_symbols = sorted(set(current_weights) | set(prev))
    for symbol in all_symbols:
        curr = float(current_weights.get(symbol, 0.0))
        prior = float(prev.get(symbol, 0.0))
        curr_on = abs(curr) > epsilon
        prev_on = abs(prior) > epsilon

        if curr_on and not prev_on:
            action: HoldingAction = "enter"
            entered.append(symbol)
        elif not curr_on and prev_on:
            action = "exit"
            exited.append(symbol)
        elif curr_on and prev_on:
            # Classify adds/trims when weight magnitude moves meaningfully.
            if abs(curr) - abs(prior) > epsilon:
                action = "add"
            elif abs(prior) - abs(curr) > epsilon:
                action = "trim"
            else:
                action = "hold"
            held.append(symbol)
        else:
            continue

        # Persist exits too so the day ops panel can show who left with weight context.
        info = meta.get(symbol) or {}
        trend = info.get("trend")
        rows.append(
            HoldingRow(
                symbol=symbol,
                weight=curr if curr_on else 0.0,
                action=action,
                price=info.get("price"),
                data_as_of=info.get("data_as_of"),
                provisional=bool(info.get("provisional", False)),
                prev_weight=prior if prev_on else None,
                trend=trend if isinstance(trend, dict) else None,
            )
        )

    # Holdings table shows current book only; exits live in ops.exited.
    current_rows = [r for r in rows if abs(r.weight) > epsilon]
    current_rows.sort(key=lambda r: (-abs(r.weight), r.symbol))

    return current_rows, DayOps(entered=entered, exited=exited, held=held)


def previous_weights_from_holdings(holdings: Iterable[HoldingRow]) -> dict[str, float]:
    """Rebuild a weight map from a persisted snapshot's holdings list."""
    out: dict[str, float] = {}
    for row in holdings:
        symbol = str(row.symbol or "").strip().upper()
        if not symbol:
            continue
        if abs(float(row.weight)) > WEIGHT_EPSILON:
            out[symbol] = float(row.weight)
    return out
