"""Five-field hypothesis gate and machine-checkable invalidation.

A signal is tradable only when all five expert fields are present:

* observation — one objective sentence, with no causal conclusion
* hypothesis — a causal sentence (because / therefore), not a correlation
* logic — behavioral finance, market structure, or a human bias
* validation_plan — data source, window, metrics, and cost assumptions
* invalidation — the condition that kills the idea, preferably machine-checkable

Machine-checkable invalidation is one of ``price`` (a side and a level),
``deviation`` (a cumulative paper-versus-backtest gap), or ``decay`` (a
relative Sharpe drop). Free text still satisfies the field. The structured
rule is what the monitor and the paper book can trip on their own.
"""

from __future__ import annotations

from typing import Any

FIVE_FIELDS = (
    "observation",
    "hypothesis",
    "logic",
    "validation_plan",
    "invalidation",
)

MACHINE_KINDS = ("price", "deviation", "decay")

# Markers that turn a sentence into a conclusion. Observation must not carry
# one. The causal hypothesis must carry one, in English or Chinese.
_CAUSAL_MARKERS = (
    "because",
    "therefore",
    "hence",
    "thus",
    "leads to",
    "causes",
    "caused by",
    "因为",
    "因此",
    "所以",
    "导致",
    "从而",
)


def looks_causal(text: str) -> bool:
    """True when ``text`` states a cause, not only a co-movement."""
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _CAUSAL_MARKERS)


def assess_five_fields(
    *,
    observation: str,
    hypothesis: str,
    logic: str,
    validation_plan: str,
    invalidation: str,
) -> list[str]:
    """Return stable reason codes. Empty means the five fields can be tradable."""
    reasons: list[str] = []
    if not observation:
        reasons.append("missing_observation")
    elif looks_causal(observation):
        reasons.append("observation_not_objective")
    if not hypothesis:
        reasons.append("missing_hypothesis")
    elif not looks_causal(hypothesis):
        reasons.append("hypothesis_not_causal")
    if not logic:
        reasons.append("missing_logic")
    if not validation_plan:
        reasons.append("missing_validation_plan")
    if not invalidation:
        reasons.append("missing_invalidation")
    return reasons


def _positive(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def parse_machine_invalidation(
    *,
    kind: str = "",
    side: str = "",
    price: Any = None,
    level: Any = None,
) -> tuple[str, str, float | None]:
    """Return ``(kind, side, level)`` or raise when the rule is partial.

    A blank rule (no kind, side, price, or level) is free text and returns
    ``("", "", None)``. ``price`` is the level for a price rule; ``level`` is
    the threshold for deviation and decay, and an alias for the price.
    """
    raw_kind = str(kind or "").strip().lower()
    raw_side = str(side or "").strip().lower()
    if raw_kind and raw_kind not in MACHINE_KINDS:
        allowed = ", ".join(MACHINE_KINDS)
        raise ValueError(f"invalidation_kind must be one of: {allowed}")
    if raw_side and raw_side not in ("below", "above"):
        raise ValueError("invalidation_side must be 'below' or 'above'")

    price_set = price is not None and not (isinstance(price, str) and not str(price).strip())
    level_set = level is not None and not (isinstance(level, str) and not str(level).strip())
    if not raw_kind and not raw_side and not price_set and not level_set:
        return "", "", None

    parsed_price = _positive(price) if price_set else None
    parsed_level = _positive(level) if level_set else None
    if price_set and parsed_price is None:
        raise ValueError("invalidation_price must be a positive number")
    if level_set and parsed_level is None:
        raise ValueError("invalidation_level must be a positive number")

    resolved_kind = raw_kind or ("price" if raw_side or price_set else "")
    if not resolved_kind:
        raise ValueError("invalidation_kind is required when a threshold is set")
    if resolved_kind == "price":
        resolved_side = raw_side
        resolved_level = parsed_price if parsed_price is not None else parsed_level
        if not resolved_side or resolved_level is None:
            raise ValueError("invalidation_side and invalidation_price must be set together")
        return "price", resolved_side, resolved_level

    if raw_side or price_set:
        raise ValueError(f"invalidation kind '{resolved_kind}' does not take a price side")
    if parsed_level is None:
        raise ValueError(f"invalidation_level is required for kind '{resolved_kind}'")
    return resolved_kind, "", parsed_level
