"""Tradable requires the five hypothesis fields."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.accessor import reset_env_config
from src.hypotheses.fields import assess_five_fields, looks_causal
from src.hypotheses.journal import DecisionJournal
from src.hypotheses.registry import HypothesisRegistry
from tests.journal_fields import FIVE_FIELDS


@pytest.fixture()
def journal_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "decision_journal.json"
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(path))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hypotheses.json"))
    reset_env_config()
    return path


def _base(**overrides: str) -> dict[str, str]:
    payload = {
        "run_id": "run_demo",
        "thesis": "Gap names fade.",
        "planned_position": "2% NAV",
        "planned_risk": "0.4% NAV",
        "invalidation": "Exit on a close above the gap high.",
        **FIVE_FIELDS,
    }
    payload.update(overrides)
    return payload


def test_causal_marker_splits_observation_and_hypothesis() -> None:
    assert looks_causal("Because supply showed up, therefore the close faded.")
    assert not looks_causal("The close finished above the prior high.")
    assert assess_five_fields(
        observation="Because buyers chased it, therefore it will fall.",
        hypothesis="Prices and volume moved together.",
        logic="A bias.",
        validation_plan="Daily bars, one year, Sharpe, costs on.",
        invalidation="A stop.",
    ) == ["observation_not_objective", "hypothesis_not_causal"]


def test_missing_observation_is_not_tradable_and_strict_rejects(journal_path: Path) -> None:
    journal = DecisionJournal()
    signal = journal.record(**_base(observation=""))
    assert signal.tradable is False
    assert "missing_observation" in signal.block_reasons

    with pytest.raises(ValueError, match="missing_logic"):
        journal.record(**_base(logic="  "), strict=True)
    assert len(journal.list()) == 1


def test_registry_round_trips_five_fields_and_a_decay_rule(journal_path: Path) -> None:
    registry = HypothesisRegistry()
    created = registry.create(
        title="Gap fade",
        thesis="Gaps fade.",
        **FIVE_FIELDS,
        invalidation="Exit on a close above the high.",
        invalidation_kind="decay",
        invalidation_level=0.4,
    )
    loaded = registry.list()[0]
    assert loaded.hypothesis_id == created.hypothesis_id
    assert loaded.observation == FIVE_FIELDS["observation"]
    assert "therefore" in loaded.hypothesis
    assert loaded.invalidation_kind == "decay"
    assert loaded.invalidation_level == pytest.approx(0.4)
    assert loaded.invalidation_notes == loaded.invalidation
