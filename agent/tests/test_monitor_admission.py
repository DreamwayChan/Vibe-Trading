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
