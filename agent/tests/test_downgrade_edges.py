"""Branches of the paper-book downgrade that a happy session does not hit."""

from __future__ import annotations

import copy
import json
import logging

import pytest

from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.paper_book.downgrade import clear_for_decay, downgrade_admission
from src.paper_book.models import PAPER_BOOK_DECAY, PAPER_BOOK_DEVIATION


def _config() -> MonitorConfig:
    return MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run1"),
        suggestions=[
            MonitorSuggestion(
                symbol="000001.SZ",
                action="buy",
                reason="signal",
                as_of="2026-01-05",
                tradable=True,
            )
        ],
        tradable=True,
        tradable_reasons=[PAPER_BOOK_DEVIATION],
    )


class _Artifact:
    def __init__(self, status: str = "monitoring", gate: object = None) -> None:
        self.id = "art-1"
        self.run_dir = "run1"
        self.status = status
        self.validation_gate = gate
        self.bars_source = "lake"


def test_a_second_reason_is_kept_and_a_dead_store_does_not_raise() -> None:
    config = _config()

    class Down:
        def list_artifacts_for_run(self, *args, **kwargs):
            raise RuntimeError("store down")

    ids = downgrade_admission(
        config,
        run_key="run1",
        run_dir="run1",
        deviation=0.2,
        threshold=0.1,
        as_of="2026-01-05",
        strategy_store=Down(),
        reason=PAPER_BOOK_DECAY,
    )
    assert ids == []
    assert config.tradable is False
    assert config.tradable_reasons == [PAPER_BOOK_DEVIATION, PAPER_BOOK_DECAY]
    assert config.suggestions[0].tradable is False
    assert PAPER_BOOK_DECAY in config.suggestions[0].block_reasons


def test_record_gate_failure_is_skipped_and_a_status_change_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    config = _config()
    seen: list[dict] = []

    class Store:
        def list_artifacts_for_run(self, *args, **kwargs):
            return [
                _Artifact(gate='{"paper_book": {"reasons": ["paper_book_deviation"], "return_deviation": 0.2}}'),
                _Artifact(),
            ]

        def record_gate(self, artifact_id, **kwargs):
            seen.append(copy.deepcopy(kwargs))
            if len(seen) == 1:
                raise RuntimeError("write failed")
            updated = _Artifact(status="disabled")
            return updated

    with caplog.at_level(logging.ERROR):
        ids = downgrade_admission(
            config,
            run_key="run1",
            run_dir="run1",
            deviation=0.4,
            threshold=0.1,
            as_of="2026-01-06",
            strategy_store=Store(),
            reason=PAPER_BOOK_DECAY,
            extra={"decay_reason": "sign_flip"},
        )
    assert ids == ["art-1"]
    gate = seen[0]["validation_gate"]
    assert gate["paper_book"]["reasons"] == [PAPER_BOOK_DEVIATION, PAPER_BOOK_DECAY]
    assert gate["paper_book"]["return_deviation"] == 0.2
    assert gate["paper_book"]["value"] == 0.4
    assert gate["paper_book"]["decay_reason"] == "sign_flip"
    assert "changed lifecycle status" in caplog.text


def test_merge_gate_accepts_a_raw_string_and_a_non_dict() -> None:
    config = _config()
    captured: list[dict] = []

    class Store:
        def __init__(self, gate: object) -> None:
            self.gate = gate

        def list_artifacts_for_run(self, *args, **kwargs):
            return [_Artifact(gate=self.gate)]

        def record_gate(self, artifact_id, **kwargs):
            captured.append(kwargs["validation_gate"])
            return _Artifact()

    downgrade_admission(
        config,
        run_key="run1",
        run_dir="run1",
        deviation=0.2,
        threshold=0.1,
        as_of="2026-01-05",
        strategy_store=Store("not-json"),
    )
    assert captured[0]["paper_book"]["reason"] == PAPER_BOOK_DEVIATION
    assert "raw" in captured[0]

    downgrade_admission(
        config,
        run_key="run1",
        run_dir="run1",
        deviation=0.2,
        threshold=0.1,
        as_of="2026-01-05",
        strategy_store=Store("[1, 2]"),
    )
    assert captured[1]["raw"] == [1, 2]

    prior = json.dumps({"paper_book": {"reason": "invalidated"}})
    downgrade_admission(
        config,
        run_key="run1",
        run_dir="run1",
        deviation=0.2,
        threshold=0.1,
        as_of="2026-01-05",
        strategy_store=Store(prior),
    )
    assert captured[2]["paper_book"]["reasons"][0] == "invalidated"


def test_clear_for_decay_respects_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    assert clear_for_decay(
        config,
        {"decayed": False},
        run_key="run1",
        run_dir=None,
        as_of="2026-01-05",
        strategy_store=None,
    ) is False
    assert config.tradable is True

    monkeypatch.setattr("src.paper_book.downgrade.decay_clears_tradable", lambda: False)
    assert clear_for_decay(
        config,
        {"decayed": True, "decay": "bad", "max_decay": None},
        run_key="run1",
        run_dir=None,
        as_of="2026-01-05",
        strategy_store=None,
    ) is False

    monkeypatch.setattr("src.paper_book.downgrade.decay_clears_tradable", lambda: True)
    assert clear_for_decay(
        config,
        {"decayed": True, "decay": "bad", "max_decay": "also-bad", "reason": "sign_flip"},
        run_key="run1",
        run_dir=None,
        as_of="2026-01-05",
        strategy_store=None,
    ) is True
    assert config.tradable is False
    assert PAPER_BOOK_DECAY in config.tradable_reasons
