"""Outcome review: realised PnL recorded against decision-journal entries."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config.accessor import reset_env_config
from src.hypotheses.journal import DecisionJournal, outcome_summary
from tests.journal_fields import FIVE_FIELDS
from tests.test_monitor_routes import _bind_run, client, home, store  # noqa: F401


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DecisionJournal:
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(tmp_path / "journal.json"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hyp.json"))
    reset_env_config()
    return DecisionJournal()


def _record(book: DecisionJournal, **kw: object) -> object:
    payload = {
        "run_id": "run_x",
        "thesis": "t",
        "invalidation": "i",
        "planned_position": "p",
        "planned_risk": "r",
        **FIVE_FIELDS,
    }
    payload.update(kw)
    return book.record(**payload)


def test_record_outcome_persists_and_summarises(journal: DecisionJournal) -> None:
    win = _record(journal)
    loss = _record(journal)
    _record(journal)

    updated = journal.record_outcome(win.signal_id, pnl_pct=4.0, note="took profit")
    journal.record_outcome(loss.signal_id, pnl_pct=-2.0)

    assert updated.outcome and updated.outcome["note"] == "took profit"
    assert updated.outcome["recorded_at"].endswith("Z")
    summary = outcome_summary(journal.list())
    assert summary == {"reviewed": 2, "wins": 1, "win_rate": 0.5, "mean_pnl_pct": 1.0}
    assert outcome_summary([]) == {"reviewed": 0, "wins": 0, "win_rate": None, "mean_pnl_pct": None}


def test_record_outcome_rejects_unknown_id_and_bad_number(journal: DecisionJournal) -> None:
    signal = _record(journal)
    with pytest.raises(KeyError):
        journal.record_outcome("sig_missing", pnl_pct=1.0)
    with pytest.raises(ValueError):
        journal.record_outcome(signal.signal_id, pnl_pct=float("nan"))


def test_outcome_routes(client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _bind_run(home, monkeypatch, "20260101_000000_00_out01")
    client.post("/monitor/signal", json={"thesis": "t"})
    entry = client.get("/monitor/journal").json()[0]

    done = client.post(
        f"/monitor/journal/{entry['signal_id']}/outcome", json={"pnl_pct": -1.5, "note": "stopped"}
    )
    assert done.status_code == 200
    assert done.json()["outcome"]["pnl_pct"] == -1.5
    assert client.get("/monitor/journal").json()[0]["outcome"]["note"] == "stopped"
    summary = client.get("/monitor/journal/summary").json()
    assert summary["reviewed"] == 1
    assert summary["wins"] == 0
    assert client.post("/monitor/journal/sig_nope/outcome", json={"pnl_pct": 1}).status_code == 404
