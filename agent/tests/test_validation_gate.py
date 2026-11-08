"""Cost-aware validation gate: deny and allow against MC / bootstrap / walk-forward."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from backtest.validation import (
    ValidationThresholds,
    config_is_cost_aware,
    evaluate_run_validation,
    evaluate_validation_gate,
    run_validation,
    thresholds_from_env,
)
from src.config.accessor import reset_env_config
from src.strategy_store.models import Artifact, ArtifactType
from src.strategy_store.sqlite_store import SqliteStrategyStore
from src.strategy_store.store import InMemoryStrategyStore, StrategyStoreProtocol


def _passing(**overrides: object) -> dict:
    payload: dict = {
        "monte_carlo": {"p_value_sharpe": 0.01},
        "bootstrap": {"ci_lower": 0.25},
        "walk_forward": {"consistency_rate": 0.8},
        "cost_aware": True,
    }
    payload.update(overrides)
    return payload


def _ashare_config() -> dict:
    return {"source": "local", "codes": ["600519.SH"], "interval": "1D"}


@pytest.fixture(autouse=True)
def _reset_env() -> None:
    reset_env_config()
    yield  # type: ignore[misc]
    reset_env_config()


def test_gate_allows_cost_aware_pass() -> None:
    gate = evaluate_validation_gate(_passing(), config=_ashare_config())
    assert gate.passed is True
    assert gate.reasons == []
    assert gate.cost_aware is True
    assert gate.to_dict()["checks"]["monte_carlo"]["passed"] is True


def test_gate_denies_high_monte_carlo_p_value() -> None:
    results = _passing(monte_carlo={"p_value_sharpe": 0.42})
    gate = evaluate_validation_gate(results, config=_ashare_config())
    assert gate.passed is False
    assert gate.reasons == ["monte_carlo_p_value"]


def test_gate_denies_negative_bootstrap_ci() -> None:
    results = _passing(bootstrap={"ci_lower": -0.2})
    gate = evaluate_validation_gate(results, config=_ashare_config())
    assert "bootstrap_ci_lower" in gate.reasons
    assert gate.passed is False


def test_gate_denies_weak_walk_forward() -> None:
    results = _passing(walk_forward={"consistency_rate": 0.4})
    gate = evaluate_validation_gate(results, config=_ashare_config())
    assert gate.reasons == ["walk_forward_consistency"]


def test_missing_artifact_is_denied() -> None:
    gate = evaluate_validation_gate(None, config=_ashare_config())
    assert gate.passed is False
    assert gate.reasons == ["missing_validation"]
    # No artifact means no cost evidence: a missing file must not be reported as
    # "costs not applied", which is what a reader would otherwise see.
    assert "costs" not in gate.checks


def test_zero_cost_crypto_is_denied_even_with_strong_stats() -> None:
    gate = evaluate_validation_gate(
        _passing(cost_aware=False),
        config={"source": "okx", "codes": ["BTC-USDT"]},
    )
    assert gate.passed is False
    assert "costs_not_applied" in gate.reasons


def test_explicit_zero_fees_opt_out_of_ashare_defaults() -> None:
    config = {
        "codes": ["600519.SH"],
        "source": "local",
        "commission_rate": 0,
        "commission_min": 0,
        "stamp_tax": 0,
        "transfer_fee": 0,
        "slippage": 0,
    }
    assert config_is_cost_aware(config) is False
    gate = evaluate_validation_gate(_passing(cost_aware=False), config=config)
    assert "costs_not_applied" in gate.reasons


def test_ashare_defaults_count_as_cost_aware_without_explicit_fees() -> None:
    assert config_is_cost_aware(_ashare_config()) is True
    gate = evaluate_validation_gate(_passing(cost_aware=False), config=_ashare_config())
    assert gate.passed is True
    assert gate.cost_aware is True


def test_env_override_relaxes_p_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_VAL_MAX_P_SHARPE", "0.50")
    reset_env_config()
    rules = thresholds_from_env()
    assert rules.max_p_value_sharpe == 0.50
    gate = evaluate_validation_gate(
        _passing(monte_carlo={"p_value_sharpe": 0.42}),
        config=_ashare_config(),
    )
    assert gate.passed is True


def test_disabling_the_gate_skips_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_VAL_GATE", "0")
    reset_env_config()
    gate = evaluate_validation_gate(None, config={"source": "okx", "codes": ["BTC-USDT"]})
    assert gate.passed is True
    assert gate.checks["enabled"] is False
    assert gate.checks["note"] == "gate_disabled"


def test_section_can_be_turned_off() -> None:
    rules = ValidationThresholds(require_monte_carlo=False)
    gate = evaluate_validation_gate(
        {
            "bootstrap": {"ci_lower": 0.2},
            "walk_forward": {"consistency_rate": 0.8},
            "cost_aware": True,
        },
        config=_ashare_config(),
        thresholds=rules,
    )
    assert gate.passed is True
    assert gate.checks["monte_carlo"]["skipped"] is True


def test_run_validation_stamps_cost_aware_and_empty_config_stays_empty() -> None:
    equity = pd.Series(
        [100.0, 101.0, 102.0, 103.0],
        index=pd.bdate_range("2024-01-02", periods=4),
    )
    stamped = run_validation(
        {
            "validation": {"walk_forward": {"n_windows": 1}},
            **_ashare_config(),
        },
        equity,
        [],
        1_000_000,
    )
    assert stamped["cost_aware"] is True
    assert "costs" in stamped
    assert run_validation({}, equity, [], 1_000_000) == {}


def test_load_run_dir_denies_when_artifact_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_x"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps(_ashare_config()), encoding="utf-8")
    gate = evaluate_run_validation(run_dir)
    assert gate.reasons == ["missing_validation"]
    assert gate.cost_aware is True


def test_memory_and_sqlite_surface_gate(tmp_path: Path) -> None:
    payload = evaluate_validation_gate(_passing(), config=_ashare_config()).to_dict()
    artifact = Artifact(
        id="",
        type=ArtifactType.STRATEGY,
        name="reversal",
        universe="csi300",
        run_dir=str(tmp_path / "run_demo"),
    )
    for store in (InMemoryStrategyStore(), SqliteStrategyStore(tmp_path / "store.db")):
        artifact_id = store.register_artifact(artifact)
        updated = store.record_gate(
            artifact_id,
            validation_gate=payload,
            bars_source="lake",
            tradable=True,
        )
        assert updated is not None
        assert updated.tradable is True
        assert updated.bars_source == "lake"
        assert json.loads(updated.validation_gate or "{}")["passed"] is True
        matched = store.list_artifacts_for_run("run_demo", run_dir=str(tmp_path / "run_demo"))
        assert [row.id for row in matched] == [artifact_id]
        assert isinstance(store, StrategyStoreProtocol)


def test_sqlite_v1_database_gains_gate_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            source_paper TEXT,
            source_url TEXT,
            formula_latex TEXT,
            theme TEXT,
            columns_required TEXT,
            decay_horizon INTEGER DEFAULT 20,
            signal_definition TEXT,
            entry_rules TEXT,
            exit_rules TEXT,
            position_sizing TEXT,
            universe TEXT NOT NULL,
            signal_engine_path TEXT,
            run_dir TEXT,
            hypothesis_id TEXT,
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            disabled_at TEXT,
            disabled_reason TEXT
        );
        CREATE TABLE bench_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT NOT NULL,
            bench_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE decay_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("PRAGMA user_version=1")
    conn.execute(
        """
        INSERT INTO artifacts (
            id, type, name, universe, theme, columns_required, status, created_at, updated_at, run_dir
        ) VALUES ('art_old', 'strategy', 'old', 'csi300', '[]', '[]', 'active', 't0', 't0', 'run_old')
        """
    )
    conn.commit()
    conn.close()

    store = SqliteStrategyStore(db_path)
    loaded = store.get_artifact("art_old")
    assert loaded is not None
    assert loaded.tradable is False
    assert loaded.bars_source is None
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2


def _equity(daily: list[float]) -> pd.Series:
    idx = pd.bdate_range("2023-01-02", periods=len(daily) + 1)
    return pd.Series([1_000_000.0, *pd.Series(daily).add(1).cumprod() * 1_000_000.0], index=idx)


def test_out_of_sample_split_flags_degradation() -> None:
    from backtest.validation import out_of_sample_split

    good = [0.002, 0.001, 0.003, -0.001] * 25
    bad = [-0.002, -0.001, 0.0005, -0.003] * 10
    res = out_of_sample_split(_equity(good + bad), oos_fraction=0.3)
    assert res["oos_sharpe"] < res["is_sharpe"]
    assert res["degradation"] > 0.5

    stable = out_of_sample_split(_equity(good * 2), oos_fraction=0.3)
    assert stable["degradation"] < 0.5


def test_out_of_sample_split_rejects_short_or_bad_fraction() -> None:
    from backtest.validation import out_of_sample_split

    assert "error" in out_of_sample_split(_equity([0.01] * 10))
    assert "error" in out_of_sample_split(_equity([0.01] * 100), oos_fraction=1.5)


def test_gate_out_of_sample_is_opt_in_and_enforced() -> None:
    base = _passing()
    assert evaluate_validation_gate(base, config=_ashare_config()).passed is True

    rules = ValidationThresholds(require_out_of_sample=True)
    missing = evaluate_validation_gate(base, config=_ashare_config(), thresholds=rules)
    assert missing.reasons == ["missing_out_of_sample"]

    bad = _passing(out_of_sample={"degradation": 0.9, "oos_sharpe": 0.1})
    gate = evaluate_validation_gate(bad, config=_ashare_config(), thresholds=rules)
    assert gate.reasons == ["out_of_sample_degraded"]

    ok = _passing(out_of_sample={"degradation": 0.2, "oos_sharpe": 1.0})
    assert evaluate_validation_gate(ok, config=_ashare_config(), thresholds=rules).passed is True
