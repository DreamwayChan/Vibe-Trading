"""Downgrade tradable-pool membership when the paper path leaves the backtest.

``record_gate`` updates ``tradable`` and the gate payload. It does not call
``update_status``, so a strategy in ``monitoring`` stays there.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.monitor.models import MonitorConfig
from src.paper_book.models import PAPER_BOOK_DEVIATION
from src.strategy_store.models import artifact_matches_run

logger = logging.getLogger(__name__)


def decay_clears_tradable() -> bool:
    """True when a decay-over-threshold should clear the tradable pool.

    Default is on. ``VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE=0`` keeps
    decay as a window-report warning.
    """
    from src.config.accessor import get_env_config

    return bool(get_env_config().data.vibe_trading_paper_book_decay_clears_tradable)


def downgrade_admission(
    config: MonitorConfig,
    *,
    run_key: str,
    run_dir: str | None,
    deviation: float,
    threshold: float,
    as_of: str,
    strategy_store: Any | None,
    reason: str = PAPER_BOOK_DEVIATION,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    """Mark the board and matching artifacts not tradable. Returns artifact ids.

    Lifecycle status is left as stored. The statistical gate's ``passed`` flag
    is left as stored; the paper-book fact is nested under ``paper_book``.
    ``reason`` is ``paper_book_deviation``, ``paper_book_decay``, or
    ``invalidated``. A later call keeps the earlier note and adds the new reason.
    """
    if reason not in config.tradable_reasons:
        config.tradable_reasons.append(reason)
    config.tradable = False
    for suggestion in config.suggestions:
        if reason not in suggestion.block_reasons:
            suggestion.block_reasons.append(reason)
        suggestion.tradable = False

    stamped: list[str] = []
    if strategy_store is None or not hasattr(strategy_store, "record_gate"):
        return stamped
    try:
        matches = strategy_store.list_artifacts_for_run(run_key, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 — a store outage must not drop the report
        logger.warning("paper book strategy-store lookup failed: %s", exc)
        return stamped
    note: dict[str, Any] = {
        "downgraded": True,
        "reason": reason,
        "threshold": threshold,
        "as_of": as_of,
    }
    if reason == PAPER_BOOK_DEVIATION:
        note["return_deviation"] = deviation
    else:
        note["value"] = deviation
    if extra:
        note.update(extra)
    for artifact in matches:
        if not artifact_matches_run(artifact, run_key, run_dir):
            continue
        status_before = artifact.status
        gate = _merge_gate(artifact.validation_gate, note)
        try:
            updated = strategy_store.record_gate(
                artifact.id,
                validation_gate=gate,
                bars_source=artifact.bars_source if artifact.bars_source is not None else config.bars_source,
                tradable=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("paper book gate downgrade failed for %s: %s", artifact.id, exc)
            continue
        if updated is None:
            continue
        if updated.status != status_before:
            logger.error(
                "paper book downgrade changed lifecycle status for %s (%s -> %s)",
                artifact.id,
                status_before,
                updated.status,
            )
        stamped.append(artifact.id)
    return stamped


def _merge_gate(existing: Any, note: dict[str, Any]) -> dict[str, Any]:
    if isinstance(existing, str) and existing.strip():
        try:
            parsed = json.loads(existing)
        except json.JSONDecodeError:
            parsed = {"raw": existing}
    elif isinstance(existing, dict):
        parsed = dict(existing)
    else:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"raw": parsed}
    previous = parsed.get("paper_book")
    reasons: list[str] = []
    if isinstance(previous, dict):
        prior = previous.get("reasons")
        if isinstance(prior, list):
            reasons.extend(str(item) for item in prior if str(item).strip())
        prior_reason = str(previous.get("reason") or "").strip()
        if prior_reason and prior_reason not in reasons:
            reasons.append(prior_reason)
        if "return_deviation" not in note and previous.get("return_deviation") is not None:
            note["return_deviation"] = previous.get("return_deviation")
    if note["reason"] not in reasons:
        reasons.append(str(note["reason"]))
    note["reasons"] = reasons
    parsed["paper_book"] = note
    return parsed


def clear_for_decay(
    config: MonitorConfig,
    decay: dict[str, Any],
    *,
    run_key: str,
    run_dir: str | None,
    as_of: str,
    strategy_store: Any | None,
) -> bool:
    """Clear tradable when decay is over the threshold and the switch is on.

    Returns True when this call downgraded the board. A decay payload that is
    not flagged, or a switch set to off, leaves the board alone.
    """
    if not decay.get("decayed"):
        return False
    if not decay_clears_tradable():
        return False
    try:
        value = float(decay.get("decay") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    try:
        threshold = float(decay.get("max_decay") or 0.0)
    except (TypeError, ValueError):
        threshold = 0.0
    downgrade_admission(
        config,
        run_key=run_key,
        run_dir=run_dir,
        deviation=value,
        threshold=threshold,
        as_of=as_of,
        strategy_store=strategy_store,
        reason="paper_book_decay",
        extra={"decay_reason": decay.get("reason")},
    )
    return True
