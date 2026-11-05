"""Data models for strategy development artifacts and decay monitoring."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ArtifactType(str, Enum):
    """Type of research artifact."""

    FACTOR = "factor"
    STRATEGY = "strategy"


class ArtifactStatus(str, Enum):
    """Lifecycle states for a research artifact."""

    CREATED = "created"
    BENCHING = "benching"
    ACTIVE = "active"
    MONITORING = "monitoring"
    DECAYED = "decayed"
    DISABLED = "disabled"


class DecaySignal(str, Enum):
    """Decay health signal for an artifact."""

    HEALTHY = "healthy"
    WARNING = "warning"
    DECAYED = "decayed"
    CRITICAL = "critical"


class BenchCategory(str, Enum):
    """Backtest evaluation category."""

    ALIVE = "alive"
    REVERSED = "reversed"
    DEAD = "dead"
    CONFIRMED_ALIVE = "confirmed_alive"
    NOISE = "noise"


@dataclass(frozen=True)
class Artifact:
    """Persisted research artifact record."""

    id: str
    type: ArtifactType
    name: str
    universe: str
    source_paper: str | None = None
    source_url: str | None = None
    # Factor-specific
    formula_latex: str | None = None
    theme: tuple[str, ...] = ()
    columns_required: tuple[str, ...] = ()
    decay_horizon: int = 20
    # Strategy-specific
    signal_definition: str | None = None
    entry_rules: str | None = None
    exit_rules: str | None = None
    position_sizing: str | None = None
    # Common
    signal_engine_path: str | None = None
    run_dir: str | None = None
    hypothesis_id: str | None = None
    # Lifecycle
    status: ArtifactStatus = ArtifactStatus.CREATED
    created_at: str = ""
    updated_at: str = ""
    disabled_at: str | None = None
    disabled_reason: str | None = None
    # Monitor tradable-pool gate. ``validation_gate`` is a JSON object (text).
    # ``tradable`` is pool membership, distinct from decay status ``monitoring``.
    validation_gate: str | None = None
    bars_source: str | None = None
    tradable: bool = False


def gate_payload_text(value: Any) -> str | None:
    """Serialize a validation-gate payload for the artifact column."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def artifact_matches_run(
    artifact: Artifact,
    run_key: str,
    run_dir: str | None = None,
) -> bool:
    """True when an artifact's ``run_dir`` is this monitor run.

    Matches the stored path, its final path segment (the run id), or the
    scan's absolute run directory.
    """
    stored = (artifact.run_dir or "").strip().rstrip("/\\")
    if not stored:
        return False
    key = (run_key or "").strip()
    names = {stored, Path(stored).name}
    if key and key in names:
        return True
    if run_dir:
        right = str(run_dir).strip().rstrip("/\\")
        if right and (stored == right or Path(stored).name == Path(right).name):
            return True
    return False


@dataclass(frozen=True)
class BenchResult:
    """Backtest result for an artifact."""

    id: int | None = None
    artifact_id: str = ""
    bench_type: str = "initial"
    # Factor metrics
    ic_mean: float | None = None
    ic_std: float | None = None
    ir: float | None = None
    ic_positive_ratio: float | None = None
    t_stat: float | None = None
    # Strategy metrics
    sharpe: float | None = None
    annual_return: float | None = None
    max_drawdown: float | None = None
    calmar: float | None = None
    # Evaluation
    category: BenchCategory | None = None
    # Time window
    train_start: str | None = None
    train_end: str | None = None
    test_start: str | None = None
    test_end: str | None = None
    run_dir: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class DecaySnapshot:
    """Point-in-time decay monitoring snapshot."""

    id: int | None = None
    artifact_id: str = ""
    rolling_ic_mean: float | None = None
    rolling_ir: float | None = None
    baseline_ic_mean: float | None = None
    ic_ratio: float | None = None
    decay_signal: DecaySignal | None = None
    consecutive_warnings: int = 0
    detail: str | None = None
    created_at: str = ""
