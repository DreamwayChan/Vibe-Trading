"""Alert when a held symbol has no tradable decision-journal entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config.accessor import reset_env_config
from src.hypotheses.journal import DecisionJournal
from src.monitor.admission import holdings_without_journal
from src.monitor.models import HoldingRow
from tests.journal_fields import FIVE_FIELDS


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DecisionJournal:
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(tmp_path / "journal.json"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hyp.json"))
    reset_env_config()
    return DecisionJournal()


_FULL = {
    "thesis": "t",
    "invalidation": "i",
    "planned_position": "p",
    "planned_risk": "r",
    **FIVE_FIELDS,
}


def _row(symbol: str, action: str = "hold", price: float | None = 10.0) -> HoldingRow:
    return HoldingRow(symbol=symbol, weight=0.1, action=action, price=price)  # type: ignore[arg-type]


def test_every_holding_alerts_without_any_entry(journal: DecisionJournal) -> None:
    rows = [_row("AAA"), _row("BBB"), _row("CCC", action="exit")]
    assert holdings_without_journal("run_x", rows, journal=journal) == ["AAA", "BBB"]


def test_strategy_entry_covers_all_and_symbol_entry_overrides(journal: DecisionJournal) -> None:
    journal.record(run_id="run_x", **_FULL)
    journal.record(run_id="run_x", symbol="BBB", thesis="only thesis")

    assert holdings_without_journal("run_x", [_row("AAA"), _row("BBB")], journal=journal) == ["BBB"]


def test_stale_and_invalidated_entries_do_not_cover(journal: DecisionJournal) -> None:
    journal.record(run_id="run_x", symbol="OLD", timestamp="2020-01-01T00:00:00Z", **_FULL)
    journal.record(
        run_id="run_x",
        symbol="BRK",
        invalidation_side="below",
        invalidation_price=100,
        **_FULL,
    )
    journal.record(run_id="run_x", symbol="OK", **_FULL)

    rows = [_row("OLD"), _row("BRK", price=90.0), _row("OK")]
    assert holdings_without_journal("run_x", rows, journal=journal) == ["OLD", "BRK"]


def test_no_run_key_or_holdings_means_no_alerts(journal: DecisionJournal) -> None:
    assert holdings_without_journal("", [_row("AAA")], journal=journal) == []
    assert holdings_without_journal("run_x", [], journal=journal) == []
