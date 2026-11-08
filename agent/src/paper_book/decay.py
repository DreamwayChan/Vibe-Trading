"""Edge-decay monitor for the paper book.

Factor edges fade as they get crowded or the market regime shifts. This
compares the book's recent daily returns with its earlier ones, and with the
bound backtest's expected daily returns. It is informational: it flags the
window report and never changes ``tradable`` by itself.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

DEFAULT_WINDOW = 10
DEFAULT_MAX_DECAY = 0.5


def _floats(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def _sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std < 1e-12:
        return None
    return mean / std * math.sqrt(252)


def edge_decay(
    days: Sequence[Mapping[str, Any]],
    *,
    window: int = DEFAULT_WINDOW,
    max_decay: float = DEFAULT_MAX_DECAY,
) -> dict[str, Any]:
    """Recent ``window`` sessions versus the sessions before them.

    ``decay`` is the relative Sharpe drop (1 - recent / earlier) when the
    earlier Sharpe is positive. ``decayed`` is true when it exceeds
    ``max_decay``, or when the earlier segment earned and the recent one lost
    money. ``recent_gap_mean`` is the mean paper-minus-backtest daily return.
    """
    payload: dict[str, Any] = {
        "available": False,
        "window": window,
        "max_decay": max_decay,
        "decayed": False,
        "reason": None,
    }
    returns = [row.get("daily_return") for row in days]
    if window < 2 or len(returns) < 2 * window:
        payload["reason"] = "insufficient_sessions"
        return payload
    recent_raw = returns[-window:]
    earlier_raw = returns[:-window]
    recent = _floats(recent_raw)
    earlier = _floats(earlier_raw)
    if len(recent) < window or len(earlier) < window:
        payload["reason"] = "missing_daily_returns"
        return payload

    recent_sharpe = _sharpe(recent)
    earlier_sharpe = _sharpe(earlier)
    gaps = _floats([(row.get("expectation") or {}).get("daily_return_gap") for row in days[-window:]])
    payload.update(
        {
            "available": True,
            "recent_mean": sum(recent) / len(recent),
            "earlier_mean": sum(earlier) / len(earlier),
            "recent_sharpe": recent_sharpe,
            "earlier_sharpe": earlier_sharpe,
            "recent_gap_mean": sum(gaps) / len(gaps) if gaps else None,
            "decay": None,
        }
    )
    if earlier_sharpe is not None and earlier_sharpe > 0:
        recent_value = recent_sharpe if recent_sharpe is not None else 0.0
        decay = 1.0 - recent_value / earlier_sharpe
        payload["decay"] = decay
        if decay > max_decay:
            payload["decayed"] = True
            payload["reason"] = "sharpe_decay"
    elif payload["earlier_mean"] > 0 and payload["recent_mean"] < 0:
        payload["decayed"] = True
        payload["reason"] = "return_turned_negative"
    return payload
