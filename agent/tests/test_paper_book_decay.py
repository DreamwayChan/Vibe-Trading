"""Edge-decay monitor for the paper book."""

from __future__ import annotations

from src.paper_book.decay import edge_decay


def _days(returns: list[float], gap: float = 0.0) -> list[dict]:
    return [{"daily_return": r, "expectation": {"daily_return_gap": gap}} for r in returns]


def test_short_window_is_unavailable() -> None:
    out = edge_decay(_days([0.01] * 5), window=10)
    assert out["available"] is False
    assert out["reason"] == "insufficient_sessions"


def test_sharpe_decay_is_flagged() -> None:
    good = [0.01, 0.012, 0.008, 0.011] * 3
    weak = [0.001, -0.002, 0.002, -0.001, 0.0015, -0.001, 0.0005, -0.0005, 0.001, -0.002]
    out = edge_decay(_days(good + weak), window=10)
    assert out["available"] is True
    assert out["decayed"] is True
    assert out["reason"] == "sharpe_decay"


def test_stable_edge_is_not_flagged() -> None:
    series = [0.01, 0.012, 0.008, 0.011] * 6
    out = edge_decay(_days(series, gap=-0.0001), window=10)
    assert out["decayed"] is False
    assert out["recent_gap_mean"] == -0.0001


def test_missing_returns_not_available() -> None:
    days = _days([0.01] * 20)
    days[3]["daily_return"] = None
    out = edge_decay(days, window=10)
    assert out["available"] is False
    assert out["reason"] == "missing_daily_returns"
