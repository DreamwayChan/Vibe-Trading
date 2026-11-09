"""Admit a strategy to the monitor tradable pool.

Pool membership needs both:

* a decision-journal signal with the five hypothesis fields (observation,
  causal hypothesis, logic, validation plan, invalidation), a thesis or
  hypothesis link, planned position, planned risk, and a timestamp;
* a cost-aware validation artifact that clears the Monte Carlo, bootstrap,
  and walk-forward thresholds.

A per-symbol price invalidation that the latest suggestion price has crossed
clears that name (reason ``invalidated``). A strategy-wide entry has no single
price, so it is not price-checked.

Buy/sell suggestions stay visible when the pool is closed. They are marked
``tradable=false`` and carry ``block_reasons``. Hold suggestions are not
actionable signals.

A buy is also blocked when its A-share risk flags include ST, limit-up,
a one-word board, or an extreme consecutive-board streak. Those flags are
stamped by the scan from bars already in hand. Sells are not blocked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from backtest.validation import evaluate_run_validation
from src.hypotheses.journal import DecisionJournal, DecisionSignal, is_stale
from src.monitor.ashare_risk import buy_block_reasons
from src.monitor.models import MonitorConfig, MonitorSuggestion
from src.strategy_store.models import artifact_matches_run

logger = logging.getLogger(__name__)

_ACTIONABLE = {"buy", "sell"}


@dataclass
class Admission:
    """Board-level pool decision plus the gate payload to surface."""

    tradable: bool
    reasons: list[str]
    validation_gate: dict[str, Any]
    decision_signal: dict[str, Any] | None
    bars_source: str | None


def journal_key_for(strategy: Any) -> str:
    """History/journal key for a monitor strategy pointer."""
    kind = str(getattr(strategy, "type", "") or "")
    if kind == "shadow":
        shadow_id = str(getattr(strategy, "shadow_id", "") or "").strip()
        return f"shadow_{shadow_id}" if shadow_id else ""
    return str(getattr(strategy, "run_id", "") or "").strip()


def _dedupe(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for reason in reasons:
        text = str(reason or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _check_invalidation(
    entry: DecisionSignal, price: float | None
) -> DecisionSignal:
    """Downgrade a symbol entry whose structured price level is broken."""
    if not entry.breached_by(price):
        return entry
    return replace(
        entry,
        tradable=False,
        block_reasons=_dedupe([*entry.block_reasons, "invalidated"]),
    )


def _expire(entry: DecisionSignal) -> DecisionSignal:
    """Downgrade an entry older than the journal expiry window."""
    if not is_stale(entry):
        return entry
    return replace(
        entry,
        tradable=False,
        block_reasons=_dedupe([*entry.block_reasons, "stale_journal"]),
    )


def holdings_without_journal(
    run_key: str,
    holdings: list[Any],
    *,
    journal: DecisionJournal | None = None,
) -> list[str]:
    """Symbols currently held with no tradable journal entry behind them.

    A symbol is covered by its own entry, else by the strategy-wide entry. An
    entry only counts when it is tradable, not stale, and not invalidated.
    Exits are skipped. An unreadable journal yields no alerts; admission
    already reports ``journal_unreadable``.
    """
    if not run_key or not holdings:
        return []
    book = journal if journal is not None else DecisionJournal()
    try:
        strategy_entry, symbol_entries = book.latest_for(run_key)
    except ValueError:
        return []
    if strategy_entry is not None:
        strategy_entry = _expire(strategy_entry)
    missing: list[str] = []
    for row in holdings:
        if getattr(row, "action", "") == "exit":
            continue
        symbol = str(getattr(row, "symbol", "") or "")
        entry = symbol_entries.get(symbol)
        if entry is not None:
            entry = _check_invalidation(_expire(entry), getattr(row, "price", None))
        else:
            entry = strategy_entry
        if (entry is None or not entry.tradable) and symbol and symbol not in missing:
            missing.append(symbol)
    return missing


def _annotate_suggestion(
    suggestion: MonitorSuggestion,
    *,
    gate_passed: bool,
    gate_reasons: list[str],
    strategy_entry: DecisionSignal | None,
    symbol_entries: dict[str, DecisionSignal],
) -> MonitorSuggestion:
    if suggestion.action not in _ACTIONABLE:
        return replace(suggestion, tradable=False, block_reasons=["not_actionable"])

    entry = symbol_entries.get(suggestion.symbol)
    if entry is None:
        entry = strategy_entry
    reasons: list[str] = []
    if not gate_passed:
        reasons.extend(gate_reasons)
    if entry is None:
        reasons.append("missing_journal")
    elif not entry.tradable:
        reasons.extend(entry.block_reasons or ["journal_blocked"])
    if suggestion.action == "buy":
        reasons.extend(buy_block_reasons(suggestion.risk_flags))
    tradable = not reasons
    return replace(suggestion, tradable=tradable, block_reasons=_dedupe(reasons))


def evaluate_admission(
    *,
    run_key: str,
    run_dir: Path | None,
    suggestions: list[MonitorSuggestion],
    bars_source: str | None = None,
    journal: DecisionJournal | None = None,
) -> tuple[Admission, list[MonitorSuggestion]]:
    """Score journal + validation without touching the strategy store."""
    book = journal if journal is not None else DecisionJournal()
    journal_error: str | None = None
    strategy_entry: DecisionSignal | None = None
    symbol_entries: dict[str, DecisionSignal] = {}
    try:
        strategy_entry, symbol_entries = book.latest_for(run_key)
        if strategy_entry is not None:
            strategy_entry = _expire(strategy_entry)
        prices = {item.symbol: item.price for item in suggestions}
        symbol_entries = {
            sym: _check_invalidation(_expire(entry), prices.get(sym))
            for sym, entry in symbol_entries.items()
        }
    except ValueError:
        journal_error = "journal_unreadable"

    if run_dir is None:
        gate = {
            "passed": False,
            "reasons": ["missing_validation"],
            "cost_aware": False,
            "checks": {"enabled": True},
            "thresholds": {},
        }
    else:
        gate = evaluate_run_validation(run_dir).to_dict()

    gate_reasons = [str(item) for item in (gate.get("reasons") or [])]
    gate_passed = bool(gate.get("passed")) and journal_error is None
    if journal_error:
        gate_passed = False

    strategy_ok = strategy_entry is not None and strategy_entry.tradable and journal_error is None
    symbol_ok = any(entry.tradable for entry in symbol_entries.values()) and journal_error is None
    pool = gate_passed and (strategy_ok or symbol_ok)

    reasons: list[str] = []
    if journal_error:
        reasons.append(journal_error)
    if not gate.get("passed"):
        reasons.extend(gate_reasons)
    if journal_error is None and not strategy_ok and not symbol_ok:
        if strategy_entry is None and not symbol_entries:
            reasons.append("missing_journal")
        elif strategy_entry is not None and not strategy_entry.tradable:
            reasons.extend(strategy_entry.block_reasons)
        else:
            for entry in symbol_entries.values():
                if not entry.tradable:
                    reasons.extend(entry.block_reasons)

    annotated = [
        _annotate_suggestion(
            suggestion,
            gate_passed=bool(gate.get("passed")) and journal_error is None,
            gate_reasons=gate_reasons,
            strategy_entry=None if journal_error else strategy_entry,
            symbol_entries={} if journal_error else symbol_entries,
        )
        for suggestion in suggestions
    ]
    signal_payload = strategy_entry.to_dict() if strategy_entry is not None else None
    admission = Admission(
        tradable=pool,
        reasons=_dedupe(reasons),
        validation_gate=gate,
        decision_signal=signal_payload,
        bars_source=bars_source,
    )
    return admission, annotated


def stamp_strategy_store(
    store: Any,
    *,
    run_key: str,
    run_dir: Path | None,
    admission: Admission,
) -> None:
    """Write gate status, ``bars_source``, and pool membership onto matches.

    Lifecycle status (including decay ``monitoring``) is left alone. The
    tradable pool is the ``tradable`` flag.
    """
    if store is None or not hasattr(store, "record_gate"):
        return
    try:
        matches = store.list_artifacts_for_run(run_key, run_dir=str(run_dir) if run_dir else None)
    except Exception as exc:  # noqa: BLE001 — a store outage must not fail the scan
        logger.warning("strategy store lookup failed: %s", exc)
        return
    for artifact in matches:
        if not artifact_matches_run(artifact, run_key, str(run_dir) if run_dir else None):
            continue
        try:
            store.record_gate(
                artifact.id,
                validation_gate=admission.validation_gate,
                bars_source=admission.bars_source,
                tradable=admission.tradable,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("strategy store gate stamp failed for %s: %s", artifact.id, exc)


def apply_admission(
    config: MonitorConfig,
    *,
    run_key: str,
    run_dir: Path | None,
    bars_source: str | None = None,
    journal: DecisionJournal | None = None,
    strategy_store: Any | None = None,
) -> Admission:
    """Stamp pool membership onto ``config`` and, when given, the strategy store."""
    admission, suggestions = evaluate_admission(
        run_key=run_key,
        run_dir=run_dir,
        suggestions=list(config.suggestions),
        bars_source=bars_source,
        journal=journal,
    )
    config.suggestions = suggestions
    config.tradable = admission.tradable
    config.tradable_reasons = list(admission.reasons)
    config.validation_gate = admission.validation_gate
    config.decision_signal = admission.decision_signal
    config.bars_source = admission.bars_source
    if strategy_store is not None:
        stamp_strategy_store(
            strategy_store,
            run_key=run_key,
            run_dir=run_dir,
            admission=admission,
        )
    return admission


def open_strategy_store() -> Any | None:
    """Process strategy store, or None when it cannot be opened."""
    try:
        from src.strategy_store._shared import get_store

        return get_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strategy store unavailable: %s", exc)
        return None
