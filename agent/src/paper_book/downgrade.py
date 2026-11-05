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


def downgrade_admission(
    config: MonitorConfig,
    *,
    run_key: str,
    run_dir: str | None,
    deviation: float,
    threshold: float,
    as_of: str,
    strategy_store: Any | None,
) -> list[str]:
    """Mark the board and matching artifacts not tradable. Returns artifact ids.

    Lifecycle status is left as stored. The statistical gate's ``passed`` flag
    is left as stored; the paper-book fact is nested under ``paper_book``.
    """
    if PAPER_BOOK_DEVIATION not in config.tradable_reasons:
        config.tradable_reasons.append(PAPER_BOOK_DEVIATION)
    config.tradable = False
    for suggestion in config.suggestions:
        if PAPER_BOOK_DEVIATION not in suggestion.block_reasons:
            suggestion.block_reasons.append(PAPER_BOOK_DEVIATION)
        suggestion.tradable = False

    stamped: list[str] = []
    if strategy_store is None or not hasattr(strategy_store, "record_gate"):
        return stamped
    try:
        matches = strategy_store.list_artifacts_for_run(run_key, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 — a store outage must not drop the report
        logger.warning("paper book strategy-store lookup failed: %s", exc)
        return stamped
    note = {
        "downgraded": True,
        "reason": PAPER_BOOK_DEVIATION,
        "return_deviation": deviation,
        "threshold": threshold,
        "as_of": as_of,
    }
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
    parsed["paper_book"] = note
    return parsed
