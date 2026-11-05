"""Position and drawdown caps for one paper-book session.

The position cap clips a buy's target weight. The drawdown cap refuses new
buys once peak-to-mark equity has fallen through the threshold. Sells are
unchanged: a book that is down still needs to exit.
"""

from __future__ import annotations

from typing import Any

from src.paper_book.models import BookState, Intent


def session_caps(
    state: BookState,
    intents: list[Intent],
    *,
    max_position: float,
    max_drawdown: float,
    nav: float,
) -> tuple[list[Intent], list[dict[str, str]], dict[str, Any]]:
    """Clip buys and drop them when ``nav`` is too far under the peak.

    ``nav`` is the marked equity after today's sells and before today's buys.
    """
    peak = state.peak_nav if state.peak_nav and state.peak_nav > 0 else state.initial_cash
    drawdown = ((peak - nav) / peak) if peak > 0 else 0.0
    blocked = drawdown > max_drawdown
    clipped: list[str] = []
    skipped: list[dict[str, str]] = []
    kept: list[Intent] = []
    for intent in intents:
        if intent.action != "buy":
            kept.append(intent)
            continue
        if blocked:
            skipped.append(
                {"symbol": intent.symbol, "action": "buy", "reason": "drawdown_cap"}
            )
            continue
        weight = float(intent.target_weight)
        if weight > max_position:
            clipped.append(intent.symbol)
            weight = max_position
        kept.append(Intent(symbol=intent.symbol, action="buy", target_weight=weight))
    risk = {
        "max_position": max_position,
        "max_drawdown": max_drawdown,
        "peak_nav": peak,
        "nav_before_buys": nav,
        "drawdown": drawdown,
        "drawdown_blocked": blocked,
        "clipped": clipped,
    }
    return kept, skipped, risk
