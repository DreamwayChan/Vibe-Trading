"""Monitor tradable pool: journal fields and the validation gate together."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.config.accessor import reset_env_config
from src.data.lake_fetch import FetchStats
from src.hypotheses.journal import DecisionJournal
from tests.journal_fields import FIVE_FIELDS
from src.monitor.models import MonitorConfig, StrategyRef
from src.monitor.scan import run_monitor_scan
from src.monitor.store import load_day_snapshot
from src.strategy_store.models import Artifact, ArtifactType
from src.strategy_store.store import InMemoryStrategyStore

_ENGINE = "\n".join(
    [
        "import pandas as pd",
        "class SignalEngine:",
        "    def generate(self, data_map):",
        "        out = {}",
        "        for code, df in data_map.items():",
        "            s = pd.Series(0.0, index=df.index)",
        "            if len(s) >= 1:",
        "                s.iloc[-1] = 1.0",
        "            out[code] = s",
        "        return out",
    ]
)


def _write_run(root: Path, run_id: str, validation: dict) -> Path:
    run_dir = root / run_id
    code = run_dir / "code"
    code.mkdir(parents=True)
    (code / "signal_engine.py").write_text(_ENGINE, encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps({"source": "local", "interval": "1D", "codes": ["600519.SH"]}),
        encoding="utf-8",
    )
    artifacts = run_dir / "artifacts"
    artifacts.mkdir()
    (artifacts / "validation.json").write_text(
        json.dumps(validation),
        encoding="utf-8",
    )
    return run_dir


def _passing_validation() -> dict:
    return {
        "monte_carlo": {"p_value_sharpe": 0.01},
        "bootstrap": {"ci_lower": 0.3},
        "walk_forward": {"consistency_rate": 0.8},
        "cost_aware": True,
    }


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    reset_env_config()
    return tmp_path


def _patch_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    idx = pd.date_range("2024-06-01", periods=5, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000_000,
        },
        index=idx,
    )

    def fake_fetch(symbols, *, start, end, source, interval):
        return (
            {symbol: frame.copy() for symbol in symbols},
            {},
            FetchStats(requested=len(symbols), lake_bases=len(symbols)),
        )

    monkeypatch.setattr("src.monitor.scan._fetch_universe_lake_first", fake_fetch)


def test_scan_denies_pool_when_validation_fails(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260101_000000_00_deny01"
    runs = isolated_home / "runs"
    validation = _passing_validation()
    validation["monte_carlo"] = {"p_value_sharpe": 0.55}
    _write_run(runs, run_id, validation)
    DecisionJournal().record(
        run_id=run_id,
        thesis="Names revert after a gap.",
        invalidation="Exit on a close above the gap high.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.suggestions[0].action == "buy"
    assert result.suggestions[0].tradable is False
    assert "monte_carlo_p_value" in result.suggestions[0].block_reasons
    assert result.tradable is False
    assert result.bars_source == "lake"
    snap = load_day_snapshot(run_id, "2024-06-07")
    assert snap is not None
    assert snap.validation_gate is not None
    assert snap.validation_gate["passed"] is False
    assert snap.bars_source == "lake"


def test_catalog_run_stays_research_when_journal_and_gate_pass(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen 「最优」 recipe cannot enter the pool on a passing journal."""
    from src.sector_pick.catalog import CATALOG

    run_id = CATALOG[0].run_id
    runs = isolated_home / "runs"
    run_dir = _write_run(runs, run_id, _passing_validation())
    DecisionJournal().record(
        run_id=run_id,
        thesis="Because the scan ranked first, therefore the board is the one to trade.",
        invalidation="Exit if the close leaves the board.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(
        Artifact(
            id="",
            type=ArtifactType.STRATEGY,
            name="optimal-scan",
            universe="ashare",
            run_dir=str(run_dir),
        )
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
        strategy_store=store,
    )
    assert result.suggestions[0].action == "buy"
    assert result.tradable is False
    assert "uncausal_scan" in result.tradable_reasons
    assert result.suggestions[0].tradable is False
    assert "uncausal_scan" in result.suggestions[0].block_reasons
    stamped = store.get_artifact(artifact_id)
    assert stamped is not None
    assert stamped.tradable is False


def test_catalog_run_with_decision_record_keeps_pool(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shelf row does not revoke a run that entered the hypothesis loop.

    The 「最优」 shelf is rebuilt from surviving reports, so it can sweep up the
    very run a board is bound to. Being displayed there is not evidence that
    the run skipped the loop: gate pass + journal + registered hypothesis is.
    """
    from src.hypotheses.registry import HypothesisRegistry
    from src.sector_pick.catalog import CATALOG

    run_id = CATALOG[0].run_id
    runs = isolated_home / "runs"
    _write_run(runs, run_id, _passing_validation())
    hyp = HypothesisRegistry().create(
        title="cro rank persists",
        thesis="Rank persists inside the board for a week.",
        status="validated",
    )
    DecisionJournal().record(
        run_id=run_id,
        hypothesis_id=hyp.hypothesis_id,
        thesis="Rank persists inside the board for a week.",
        invalidation="Exit if the close leaves the board.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.tradable is True
    assert "uncausal_scan" not in result.tradable_reasons
    assert result.suggestions[0].tradable is True
    assert result.hypothesis is not None
    assert result.hypothesis["id"] == hyp.hypothesis_id


def test_catalog_run_with_failed_gate_stays_research(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is gate-aware: numbers that failed cannot license the shelf row."""
    from src.hypotheses.registry import HypothesisRegistry
    from src.sector_pick.catalog import CATALOG

    run_id = CATALOG[0].run_id
    runs = isolated_home / "runs"
    validation = _passing_validation()
    validation["bootstrap"] = {"ci_lower": -0.4}
    _write_run(runs, run_id, validation)
    hyp = HypothesisRegistry().create(
        title="cro rank persists",
        thesis="Rank persists inside the board for a week.",
        status="validated",
    )
    DecisionJournal().record(
        run_id=run_id,
        hypothesis_id=hyp.hypothesis_id,
        thesis="Rank persists inside the board for a week.",
        invalidation="Exit if the close leaves the board.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.tradable is False
    assert "bootstrap_ci_lower" in result.tradable_reasons
    assert "uncausal_scan" in result.tradable_reasons


def test_archived_run_stays_research_when_journal_and_gate_pass(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ARCHIVED`` means retired on purpose: the picker hides it, so must the pool.

    Without this rule a deduped run keeps ``tradable=true`` on its board and a
    rescan quietly re-promotes a source the picker no longer offers.
    """
    run_id = "20260101_000000_00_deduped"
    runs = isolated_home / "runs"
    run_dir = _write_run(runs, run_id, _passing_validation())
    (run_dir / "ARCHIVED").write_text("superseded by a better run\n", encoding="utf-8")
    DecisionJournal().record(
        run_id=run_id,
        thesis="Because the book rebalances weekly, therefore the rank persists.",
        invalidation="Exit on a close below the entry week's low.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.suggestions[0].action == "buy"
    assert result.tradable is False
    assert "archived" in result.tradable_reasons
    assert result.suggestions[0].tradable is False
    assert "archived" in result.suggestions[0].block_reasons


def test_rejected_hypothesis_keeps_pool_closed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected premise stays research even when the gate and journal pass.

    The board used to say "in the pool" while the hypothesis registry said
    rejected: numbers cannot validate a premise whose own invalidation fired.
    """
    run_id = "20260101_000000_00_rej01"
    runs = isolated_home / "runs"
    _write_run(runs, run_id, _passing_validation())
    from src.hypotheses.registry import HypothesisRegistry

    hyp = HypothesisRegistry().create(
        title="gap revert",
        thesis="Names revert after a gap.",
        status="rejected",
    )
    DecisionJournal().record(
        run_id=run_id,
        hypothesis_id=hyp.hypothesis_id,
        thesis="Names revert after a gap.",
        invalidation="Exit on a close above the gap high.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.suggestions[0].action == "buy"
    assert result.tradable is False
    assert "hypothesis_rejected" in result.tradable_reasons
    assert result.hypothesis is not None
    assert result.hypothesis["status"] == "rejected"
    assert result.suggestions[0].tradable is False
    assert "hypothesis_rejected" in result.suggestions[0].block_reasons


def test_validated_hypothesis_is_stamped_and_pool_stays_open(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp is read-only for every other status: validated still trades."""
    run_id = "20260101_000000_00_val01"
    runs = isolated_home / "runs"
    _write_run(runs, run_id, _passing_validation())
    from src.hypotheses.registry import HypothesisRegistry

    hyp = HypothesisRegistry().create(
        title="gap revert",
        thesis="Names revert after a gap.",
        status="validated",
    )
    DecisionJournal().record(
        run_id=run_id,
        hypothesis_id=hyp.hypothesis_id,
        thesis="Names revert after a gap.",
        invalidation="Exit on a close above the gap high.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.tradable is True
    assert result.tradable_reasons == []
    assert result.hypothesis is not None
    assert result.hypothesis["status"] == "validated"
    assert result.hypothesis["id"] == hyp.hypothesis_id


def test_scan_admits_pool_when_journal_and_gate_pass(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260101_000000_00_allow1"
    runs = isolated_home / "runs"
    run_dir = _write_run(runs, run_id, _passing_validation())
    DecisionJournal().record(
        run_id=run_id,
        thesis="Names revert after a gap.",
        invalidation="Exit on a close above the gap high.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(
        Artifact(
            id="",
            type=ArtifactType.STRATEGY,
            name="gap-revert",
            universe="csi300",
            run_dir=str(run_dir),
        )
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
        strategy_store=store,
    )
    assert result.tradable is True
    assert result.suggestions[0].tradable is True
    assert result.decision_signal is not None
    assert result.decision_signal["invalidation"]
    stamped = store.get_artifact(artifact_id)
    assert stamped is not None
    assert stamped.tradable is True
    assert stamped.bars_source == "lake"
    assert json.loads(stamped.validation_gate or "{}")["passed"] is True


def test_scan_downgrades_when_invalidation_is_missing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260101_000000_00_miss01"
    runs = isolated_home / "runs"
    _write_run(runs, run_id, _passing_validation())
    DecisionJournal().record(
        run_id=run_id,
        thesis="Names revert after a gap.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
    )
    _patch_fetch(monkeypatch)
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 7),
        runs_root=runs,
    )
    assert result.tradable is False
    assert "missing_invalidation" in result.tradable_reasons
    assert result.suggestions[0].tradable is False
    assert "missing_invalidation" in result.suggestions[0].block_reasons


def test_stale_journal_entry_blocks_pool_and_suggestions(
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
        thesis="t",
        invalidation="i",
        planned_position="p",
        planned_risk="r",
        timestamp="2020-01-01T00:00:00Z",
    )
    suggestion = MonitorSuggestion(
        symbol="AAPL", action="buy", position="long", reason="x", as_of="2026-01-01", price=1.0
    )
    admission, annotated = evaluate_admission(
        run_key="run_x", run_dir=None, suggestions=[suggestion], journal=journal
    )

    assert admission.tradable is False
    assert "stale_journal" in admission.reasons
    assert annotated[0].tradable is False
    assert "stale_journal" in annotated[0].block_reasons

    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_MAX_AGE_DAYS", "0")
    reset_env_config()
    admission, _ = evaluate_admission(
        run_key="run_x", run_dir=None, suggestions=[suggestion], journal=journal
    )
    assert "stale_journal" not in admission.reasons
