"""Decision journal: invalidation is required before a signal is tradable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config.accessor import reset_env_config
from src.hypotheses.journal import DecisionJournal, assess_journal_fields
from src.hypotheses.registry import HypothesisRegistry


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
        "observation": "The close finished above the prior 20-day high.",
        "hypothesis": "Because late buyers chase the gap, therefore the next closes revert.",
        "logic": "Late buyers crowd a gap and their exits supply the move back.",
        "validation_plan": "Daily lake bars, 2022-2024, Sharpe and max drawdown, A-share costs on.",
        "invalidation": "Close the book if the 20-day low breaks.",
        "planned_position": "2% NAV",
        "planned_risk": "max loss 0.5% NAV",
    }
    payload.update(overrides)
    return payload


def test_missing_invalidation_is_not_tradable(journal_path: Path) -> None:
    journal = DecisionJournal()
    signal = journal.record(**_full(invalidation="  "))

    assert signal.tradable is False
    assert "missing_invalidation" in signal.block_reasons
    stored = json.loads(journal_path.read_text(encoding="utf-8"))
    assert stored[0]["tradable"] is False
    assert stored[0]["timestamp"]


def test_strict_rejects_without_writing(journal_path: Path) -> None:
    journal = DecisionJournal()
    with pytest.raises(ValueError, match="missing_invalidation"):
        journal.record(**_full(invalidation=""), strict=True)
    assert not journal_path.exists()


def test_complete_signal_is_tradable(journal_path: Path) -> None:
    signal = DecisionJournal().record(**_full(), timestamp="2026-11-05T01:02:03Z")
    assert signal.tradable is True
    assert signal.block_reasons == []
    assert signal.timestamp == "2026-11-05T01:02:03Z"
    assert signal.signal_id.startswith("sig_")


def test_hypothesis_link_supplies_thesis_and_invalidation(journal_path: Path) -> None:
    registry = HypothesisRegistry()
    hypothesis = registry.create(
        title="A-share reversal",
        thesis="Post-limit-up names revert.",
        observation="The name closed above the prior session high.",
        hypothesis="Because the crowd chased the limit-up, therefore the next close fades.",
        logic="Limit-up buyers are late and their exits supply the fade.",
        validation_plan="Daily bars, 60 sessions, Sharpe, A-share costs on.",
        invalidation_notes="Exit if the next close makes a higher high.",
    )
    signal = DecisionJournal(registry=registry).record(
        run_id="run_demo",
        hypothesis_id=hypothesis.hypothesis_id,
        planned_position="100 shares",
        planned_risk="stop 8%",
    )
    assert signal.tradable is True
    assert signal.thesis == "Post-limit-up names revert."
    assert "higher high" in signal.invalidation
    assert signal.observation.startswith("The name closed")
    assert "therefore" in signal.hypothesis
    assert signal.hypothesis_id == hypothesis.hypothesis_id


def test_unknown_hypothesis_is_not_tradable(journal_path: Path) -> None:
    signal = DecisionJournal().record(
        **_full(),
        hypothesis_id="hyp_missing",
    )
    assert signal.tradable is False
    assert "hypothesis_not_found" in signal.block_reasons


def test_missing_position_and_risk(journal_path: Path) -> None:
    signal = DecisionJournal().record(
        run_id="run_demo",
        thesis="A thesis.",
        invalidation="A stop.",
    )
    assert "missing_position" in signal.block_reasons
    assert "missing_risk" in signal.block_reasons
    assert signal.tradable is False


def test_invalid_timestamp_downgrades(journal_path: Path) -> None:
    signal = DecisionJournal().record(**_full(), timestamp="not-a-date")
    assert signal.tradable is False
    assert "invalid_timestamp" in signal.block_reasons
    assert signal.timestamp == "not-a-date"


def test_latest_for_prefers_symbol_override(journal_path: Path) -> None:
    journal = DecisionJournal()
    journal.record(**_full())
    blocked = journal.record(
        **_full(invalidation=""),
        symbol="600519.SH",
    )
    strategy, by_symbol = journal.latest_for("run_demo")
    assert strategy is not None and strategy.tradable is True
    assert by_symbol["600519.SH"].signal_id == blocked.signal_id
    assert by_symbol["600519.SH"].tradable is False


def test_assess_lists_every_gap() -> None:
    reasons = assess_journal_fields(
        hypothesis_id="",
        thesis="",
        invalidation="",
        planned_position="",
        planned_risk="",
        timestamp="",
        hypothesis_found=False,
    )
    assert reasons == [
        "missing_observation",
        "missing_hypothesis",
        "missing_logic",
        "missing_validation_plan",
        "missing_invalidation",
        "missing_thesis",
        "missing_position",
        "missing_risk",
        "missing_timestamp",
    ]


def test_is_stale_respects_window_and_zero_disables(journal_path: Path) -> None:
    from datetime import datetime, timezone

    from src.hypotheses.journal import is_stale

    signal = DecisionJournal().record(**_full(timestamp="2026-01-01T00:00:00Z"))
    now = datetime(2026, 2, 15, tzinfo=timezone.utc)

    assert is_stale(signal, max_age_days=30, now=now) is True
    assert is_stale(signal, max_age_days=60, now=now) is False
    assert is_stale(signal, max_age_days=0, now=now) is False


def test_default_expiry_is_thirty_days_and_env_overrides(
    journal_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.hypotheses.journal import journal_max_age_days

    assert journal_max_age_days() == 30
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_MAX_AGE_DAYS", "0")
    reset_env_config()
    assert journal_max_age_days() == 0
def test_history_is_newest_first_and_filters_symbol(journal_path: Path) -> None:
    journal = DecisionJournal()
    journal.record(**_full(timestamp="2026-01-01T00:00:00Z"))
    journal.record(**_full(symbol="aapl", timestamp="2026-01-03T00:00:00Z"))
    journal.record(**_full(symbol="MSFT", timestamp="2026-01-02T00:00:00Z"))
    journal.record(**_full(run_id="other", timestamp="2026-01-04T00:00:00Z"))

    rows = journal.history("run_demo")
    assert [row.timestamp[:10] for row in rows] == ["2026-01-03", "2026-01-02", "2026-01-01"]
    assert [row.symbol for row in journal.history("run_demo", symbol="AAPL")] == ["AAPL"]
    assert len(journal.history("run_demo", limit=1)) == 1


def test_concurrent_records_do_not_lose_rows(journal_path: Path) -> None:
    import threading

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            DecisionJournal().record(**_full(run_id=f"run_{index}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(DecisionJournal().list()) == 16
    assert not list(journal_path.parent.glob("*.tmp"))


def test_concurrent_processes_do_not_lose_rows(journal_path: Path) -> None:
    import os
    import subprocess
    import sys

    code = (
        "import sys; from src.hypotheses.journal import DecisionJournal;"
        "DecisionJournal().record(run_id=sys.argv[1], thesis='t', invalidation='i',"
        "planned_position='p', planned_risk='r')"
    )
    env = {**os.environ, "VIBE_TRADING_DECISION_JOURNAL_PATH": str(journal_path)}
    procs = [
        subprocess.Popen([sys.executable, "-c", code, f"run_{i}"], env=env)
        for i in range(8)
    ]
    assert all(proc.wait() == 0 for proc in procs)
    assert len(DecisionJournal().list()) == 8
