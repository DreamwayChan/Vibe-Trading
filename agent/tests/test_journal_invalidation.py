"""Structured invalidation levels on decision-journal signals."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.accessor import reset_env_config
from src.hypotheses.journal import DecisionJournal


@pytest.fixture()
def journal_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "decision_journal.json"
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(path))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hypotheses.json"))
    reset_env_config()
    return path


def _full(**overrides: str) -> dict[str, str]:
    payload = {
        "run_id": "run_demo",
        "thesis": "Gap reversal fades within five sessions.",
        "invalidation": "Close the book if the 20-day low breaks.",
        "planned_position": "2% NAV",
        "planned_risk": "max loss 0.5% NAV",
    }
    payload.update(overrides)
    return payload


def test_structured_invalidation_rule_round_trips_and_breaches(journal_path: Path) -> None:
    journal = DecisionJournal()
    signal = journal.record(**_full(symbol="AAPL"), invalidation_side="below", invalidation_price=100)

    saved = journal.list()[0]
    assert (saved.invalidation_side, saved.invalidation_price) == ("below", 100.0)
    assert signal.breached_by(99.9) is True
    assert signal.breached_by(100.0) is False
    assert signal.breached_by(None) is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"invalidation_side": "below"},
        {"invalidation_price": 10},
        {"invalidation_side": "sideways", "invalidation_price": 10},
        {"invalidation_side": "below", "invalidation_price": -1},
        {"invalidation_side": "below", "invalidation_price": 10},  # no symbol
    ],
)
def test_incomplete_invalidation_rule_is_rejected(journal_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        DecisionJournal().record(**_full(), **kwargs)
    assert not journal_path.exists() or DecisionJournal().list() == []



def test_broken_invalidation_level_flags_symbol_invalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.monitor.admission import evaluate_admission
    from src.monitor.models import MonitorSuggestion

    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(tmp_path / "journal.json"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hyp.json"))
    reset_env_config()
    journal = DecisionJournal()
    journal.record(
        run_id="run_x",
        symbol="AAPL",
        thesis="t",
        invalidation="close below 100",
        planned_position="p",
        planned_risk="r",
        invalidation_side="below",
        invalidation_price=100,
    )

    def suggestion(price: float) -> MonitorSuggestion:
        return MonitorSuggestion(
            symbol="AAPL", action="buy", position="long", reason="x", as_of="2026-01-01", price=price
        )

    admission, annotated = evaluate_admission(
        run_key="run_x", run_dir=None, suggestions=[suggestion(95.0)], journal=journal
    )
    assert "invalidated" in admission.reasons
    assert "invalidated" in annotated[0].block_reasons
    assert annotated[0].tradable is False

    admission, annotated = evaluate_admission(
        run_key="run_x", run_dir=None, suggestions=[suggestion(105.0)], journal=journal
    )
    assert "invalidated" not in admission.reasons
    assert "invalidated" not in annotated[0].block_reasons
