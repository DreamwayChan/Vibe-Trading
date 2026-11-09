"""Shared five-field payload for tradable journal fixtures."""

from __future__ import annotations

FIVE_FIELDS = {
    "observation": "The close finished above the prior 20-day high.",
    "hypothesis": "Because late buyers chase the gap, therefore the next closes revert.",
    "logic": "Late buyers crowd a gap and their exits supply the move back.",
    "validation_plan": "Daily lake bars, 2022-2024, Sharpe and max drawdown, A-share costs on.",
}


def with_five(payload: dict | None = None, **overrides: str) -> dict:
    """Copy the five fields, then overlay ``payload`` and ``overrides``."""
    merged = dict(FIVE_FIELDS)
    if payload:
        merged.update(payload)
    merged.update(overrides)
    return merged
