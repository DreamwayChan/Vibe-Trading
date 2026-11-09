"""Refuse frozen 「最优」 scans that never entered the hypothesis loop.

Catalog recipes are research. A passing journal and a passing validation
gate do not promote them. The stable reason is ``uncausal_scan``.
"""

from __future__ import annotations

from src.monitor.models import MonitorConfig, MonitorSuggestion

UNCAUSAL_SCAN = "uncausal_scan"
_ACTIONABLE = {"buy", "sell"}


def catalog_run_ids() -> frozenset[str]:
    """Run ids of the frozen 「最优」 catalog."""
    from src.sector_pick.catalog import CATALOG

    return frozenset(row.run_id for row in CATALOG if row.run_id)


def is_uncausal_scan(run_key: str | None) -> bool:
    """True when ``run_key`` is a frozen catalog recipe, not a hypothesis run."""
    key = str(run_key or "").strip()
    if not key:
        return False
    return key in catalog_run_ids()


def stamp_research_only(payload: dict) -> dict:
    """Mark a research payload so a caller cannot read it as pool admission."""
    payload["research_only"] = True
    payload["tradable"] = False
    payload["block_reason"] = UNCAUSAL_SCAN
    return payload


def block_actionable(suggestions: list[MonitorSuggestion]) -> list[MonitorSuggestion]:
    """Clear tradable on buy and sell rows. Holds stay as they were."""
    blocked: list[MonitorSuggestion] = []
    for suggestion in suggestions:
        if suggestion.action not in _ACTIONABLE:
            blocked.append(suggestion)
            continue
        reasons = list(suggestion.block_reasons)
        if UNCAUSAL_SCAN not in reasons:
            reasons.append(UNCAUSAL_SCAN)
        suggestion.tradable = False
        suggestion.block_reasons = reasons
        blocked.append(suggestion)
    return blocked


def seal_monitor_config(config: MonitorConfig) -> MonitorConfig:
    """Force a catalog run off the tradable pool. Other runs are unchanged."""
    run_id = str(getattr(getattr(config, "strategy", None), "run_id", "") or "")
    if not is_uncausal_scan(run_id):
        return config
    config.tradable = False
    if UNCAUSAL_SCAN not in config.tradable_reasons:
        config.tradable_reasons.append(UNCAUSAL_SCAN)
    config.suggestions = block_actionable(list(config.suggestions))
    return config
