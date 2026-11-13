"""Refuse frozen 「最优」 scans that never entered the hypothesis loop.

Catalog recipes are research. A passing journal and a passing validation
gate do not promote them. The stable reason is ``uncausal_scan``.

The shelf is not a stable set of ids, though: it is rebuilt from the surviving
reports (see ``src.sector_pick.catalog``), so a run a board is bound to can be
swept onto the shelf as its theme's last surviving report. Being *displayed*
on that shelf is not evidence that the run skipped the hypothesis loop. A run
that carries a decision record — a gate pass plus a journal signal whose
``hypothesis_id`` resolves in the registry and is not rejected — keeps its
pool membership; the shelf row is only a display row for a live report.

A run without that record stays research-only, which is the case the shelf
exists to protect: picking a top-ranked recipe off the catalog and trading it
on the strength of its backtest numbers alone.
"""

from __future__ import annotations

from typing import Any

from src.monitor.models import MonitorConfig, MonitorSuggestion

UNCAUSAL_SCAN = "uncausal_scan"
_ACTIONABLE = {"buy", "sell"}


def catalog_run_ids() -> frozenset[str]:
    """Run ids of the frozen 「最优」 catalog."""
    from src.sector_pick.catalog import CATALOG

    return frozenset(row.run_id for row in CATALOG if row.run_id)


def linked_decision_record(signal: Any, hypothesis: Any) -> bool:
    """True when a journal signal is bound to a live research hypothesis.

    The id must match the registry row, and a rejected premise does not count:
    it was tested and failed, so it cannot license a shelf run back into the
    pool. Both sides are plain dicts as persisted on the board.
    """
    if not isinstance(signal, dict) or not isinstance(hypothesis, dict):
        return False
    hypothesis_id = str(signal.get("hypothesis_id") or "").strip()
    if not hypothesis_id:
        return False
    if hypothesis_id != str(hypothesis.get("id") or "").strip():
        return False
    return str(hypothesis.get("status") or "").strip() != "rejected"


def board_decision_record(config: MonitorConfig | None) -> bool:
    """Decision record as the board persisted it (gate pass + journal link).

    A hand-written schedule or a default ``tradable=true`` carries neither, so
    it stays sealed. Only admission writes these two stamps.
    """
    if config is None:
        return False
    gate = getattr(config, "validation_gate", None)
    if not isinstance(gate, dict) or not gate.get("passed"):
        return False
    return linked_decision_record(
        getattr(config, "decision_signal", None),
        getattr(config, "hypothesis", None),
    )


def is_uncausal_scan(run_key: str | None, *, decided: bool = False) -> bool:
    """True when ``run_key`` is a frozen catalog recipe with no decision record.

    ``decided`` means the run proved it went through the hypothesis loop; the
    shelf row that points at it then stops revoking its pool membership.
    """
    key = str(run_key or "").strip()
    if not key or decided:
        return False
    return key in catalog_run_ids()


def stamp_research_only(payload: dict) -> dict:
    """Mark a research payload so a caller cannot read it as pool admission."""
    payload["research_only"] = True
    payload["tradable"] = False
    payload["block_reason"] = UNCAUSAL_SCAN
    return payload


def block_actionable(
    suggestions: list[MonitorSuggestion], reason: str = UNCAUSAL_SCAN
) -> list[MonitorSuggestion]:
    """Clear tradable on buy and sell rows. Holds stay as they were."""
    blocked: list[MonitorSuggestion] = []
    for suggestion in suggestions:
        if suggestion.action not in _ACTIONABLE:
            blocked.append(suggestion)
            continue
        reasons = list(suggestion.block_reasons)
        if reason not in reasons:
            reasons.append(reason)
        suggestion.tradable = False
        suggestion.block_reasons = reasons
        blocked.append(suggestion)
    return blocked


def seal_monitor_config(config: MonitorConfig) -> MonitorConfig:
    """Force a catalog run off the tradable pool. Other runs are unchanged.

    A board that carries a decision record keeps trading; see the module
    docstring for why the shelf alone is not disqualifying.
    """
    run_id = str(getattr(getattr(config, "strategy", None), "run_id", "") or "")
    if not is_uncausal_scan(run_id, decided=board_decision_record(config)):
        return config
    config.tradable = False
    if UNCAUSAL_SCAN not in config.tradable_reasons:
        config.tradable_reasons.append(UNCAUSAL_SCAN)
    config.suggestions = block_actionable(list(config.suggestions))
    return config
