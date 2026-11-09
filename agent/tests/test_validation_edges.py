"""Validation edges: cost routing, thresholds, and malformed run directories.

``test_validation.py`` pins the three statistics, ``test_validation_gate.py`` the
pass/deny matrix on a well-formed artifact, and ``test_validation_cli.py`` the
argument parser. What is left is the surface a real bad run directory hits: an
equity file with no recognised column, a JSON file that is not an object, a
section that came back as an error rather than a number, and the cost routing
that decides whether a strategy paid anything at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.validation import (
    ValidationThresholds,
    _as_positive,
    _config_codes,
    _finite,
    _load_equity,
    _load_trades,
    _parse_run_dir,
    book_uses_ashare_defaults,
    config_is_cost_aware,
    costs_applied,
    evaluate_run_validation,
    evaluate_validation_gate,
    load_run_validation,
    thresholds_from_env,
)
from src.config.accessor import reset_env_config


@pytest.fixture(autouse=True)
def _reset_env() -> None:
    reset_env_config()
    yield  # type: ignore[misc]
    reset_env_config()


def _passing(**overrides: object) -> dict:
    payload: dict = {
        "monte_carlo": {"p_value_sharpe": 0.01},
        "bootstrap": {"ci_lower": 0.25},
        "walk_forward": {"consistency_rate": 0.8},
        "cost_aware": True,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------


class TestLoadArtifacts:
    @pytest.mark.parametrize("column", ["equity", "nav", "value"])
    def test_equity_column_names(self, tmp_path: Path, column: str) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        frame = pd.DataFrame({column: [1.0, 2.0]}, index=pd.bdate_range("2025-01-01", periods=2))
        frame.to_csv(artifacts / "equity.csv", index_label="date")

        assert list(_load_equity(tmp_path)) == [1.0, 2.0]

    def test_equity_without_a_known_column_names_the_columns_it_found(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        pd.DataFrame({"total": [1.0]}, index=[pd.Timestamp("2025-01-01")]).to_csv(
            artifacts / "equity.csv", index_label="date"
        )

        with pytest.raises(ValueError, match="equity/nav/value"):
            _load_equity(tmp_path)

    def test_empty_trade_log_is_an_empty_list(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "trades.csv").write_text(
            "timestamp,code,side,price,qty,reason,pnl,holding_days,return_pct\n",
            encoding="utf-8",
        )

        assert _load_trades(tmp_path) == []


class TestMainWithoutConfig:
    def test_default_capital_applies_when_config_json_is_absent(
        self, tmp_path: Path
    ) -> None:
        """A run directory predating ``config.json`` still validates."""
        from backtest import validation

        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        frame = pd.DataFrame(
            {"equity": [1_000_000.0] * 12},
            index=pd.bdate_range("2025-01-01", periods=12),
        )
        frame.to_csv(artifacts / "equity.csv", index_label="date")
        (artifacts / "trades.csv").write_text(
            "timestamp,code,side,price,qty,reason,pnl,holding_days,return_pct\n",
            encoding="utf-8",
        )

        results = validation.main(tmp_path)

        assert "error" in results["monte_carlo"]  # no trades to permute
        assert "observed_sharpe" in results["bootstrap"]
        assert "cost_aware" in results


def test_an_unreadable_path_exits_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permission error on the path must be a usage error, not a traceback."""
    original = Path.exists

    def _raise(self: Path) -> bool:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "exists", _raise)
    try:
        with pytest.raises(SystemExit, match="Invalid run_dir path"):
            _parse_run_dir(["validation", "runs/whatever"])
    finally:
        monkeypatch.setattr(Path, "exists", original)


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------


class TestCostHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.001, True), ("0.001", True), (0, False), ("0", False), (None, False),
         (True, False), ("abc", False), (float("inf"), False), (float("nan"), False)],
    )
    def test_as_positive(self, value: object, expected: bool) -> None:
        assert _as_positive(value) is expected

    def test_cost_keys_are_read_from_a_mapping_only(self) -> None:
        assert costs_applied({"costs": {"commission_rate": 0.00025}}) is True
        assert costs_applied({"costs": "yes"}) is False
        assert costs_applied({"costs": {"commission_rate": 0}}) is False

    def test_no_cost_evidence_falls_back_to_the_config(self) -> None:
        assert costs_applied({}, {"codes": ["600519.SH"], "source": "local"}) is True
        assert costs_applied({}, {"source": "okx", "codes": ["BTC-USDT"]}) is False

    def test_cost_aware_flag_on_the_config_is_enough(self) -> None:
        assert config_is_cost_aware({"cost_aware": True, "source": "okx"}) is True

    def test_a_broken_cost_helper_does_not_look_cost_free(self) -> None:
        """If the A-share defaults cannot be merged, the answer must be 'no'."""
        import backtest.ashare_costs as ashare_costs

        def _boom(_config: object) -> dict:
            raise ImportError("ashare_costs unavailable")

        original = ashare_costs.merge_assumptions
        ashare_costs.merge_assumptions = _boom  # type: ignore[assignment]
        try:
            assert config_is_cost_aware({"codes": ["600519.SH"], "source": "local"}) is False
        finally:
            ashare_costs.merge_assumptions = original  # type: ignore[assignment]


class TestBookRouting:
    @pytest.mark.parametrize("market", ["china_a", "a_share", "ashare", "cn", "CN"])
    def test_explicit_a_share_market(self, market: str) -> None:
        assert book_uses_ashare_defaults({"market": market}) is True

    def test_a_share_engine(self) -> None:
        assert book_uses_ashare_defaults({"engine": "ashare"}) is True

    def test_code_list_of_explicit_a_share_listings(self) -> None:
        assert book_uses_ashare_defaults({"codes": ["600519.SH", "000001.SZ"]}) is True
        assert book_uses_ashare_defaults({"codes": ["BTC-USDT"]}) is False

    def test_tushare_book_is_a_share_unless_it_lists_offshore_codes(self) -> None:
        assert book_uses_ashare_defaults({"source": "tushare"}) is True
        assert book_uses_ashare_defaults({"source": "akshare"}) is True
        assert book_uses_ashare_defaults({"source": "tushare", "codes": ["AAPL.US"]}) is False
        assert book_uses_ashare_defaults({"source": "tushare", "codes": ["0700.HK"]}) is False

    def test_codes_may_arrive_as_a_comma_separated_string(self) -> None:
        assert _config_codes({"codes": " 600519.SH , 000001.SZ ,"}) == ["600519.SH", "000001.SZ"]
        assert _config_codes({"symbols": ["AAPL.US"]}) == ["AAPL.US"]
        assert _config_codes({"codes": 5}) == []
        assert _config_codes({}) == []


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_defaults_when_the_config_layer_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.config.accessor as accessor

        def _boom() -> object:
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(accessor, "get_env_config", _boom)

        rules = thresholds_from_env()

        assert rules.max_p_value_sharpe == 0.10
        assert rules.require_costs is True

    def test_to_dict_round_trips_every_switch(self) -> None:
        rules = ValidationThresholds(require_costs=False, enabled=False)

        payload = rules.to_dict()

        assert payload["require_costs"] is False
        assert payload["enabled"] is False
        assert set(payload) == {
            "enabled",
            "max_p_value_sharpe",
            "min_bootstrap_ci_lower",
            "min_walk_forward_consistency",
            "require_costs",
            "require_monte_carlo",
            "require_bootstrap",
            "require_walk_forward",
            "require_out_of_sample",
            "max_oos_degradation",
        }


class TestFinite:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(1, 1.0), ("0.5", 0.5), (None, None), (True, None), ("abc", None),
         (float("inf"), None), (float("-inf"), None), (float("nan"), None)],
    )
    def test_finite(self, value: object, expected: float | None) -> None:
        assert _finite(value) == expected


# ---------------------------------------------------------------------------
# Gate corners
# ---------------------------------------------------------------------------


class TestGateCorners:
    def test_unreadable_artifact_is_reported_as_such(self) -> None:
        gate = evaluate_validation_gate(None, load_error="validation_unreadable")

        assert gate.passed is False
        assert gate.reasons == ["validation_unreadable"]
        assert gate.checks == {"enabled": True}

    def test_costs_can_be_waived_explicitly(self) -> None:
        rules = ValidationThresholds(require_costs=False)
        gate = evaluate_validation_gate(
            _passing(cost_aware=False),
            config={"source": "okx", "codes": ["BTC-USDT"]},
            thresholds=rules,
        )

        assert gate.passed is True
        assert gate.checks["costs"] == {"passed": True, "skipped": True}

    def test_a_section_that_is_not_an_object_counts_as_missing(self) -> None:
        gate = evaluate_validation_gate(_passing(monte_carlo="0.01"), config={"source": "local"})

        assert gate.reasons == ["missing_monte_carlo"]
        assert gate.checks["monte_carlo"] == {
            "passed": False,
            "reason": "missing_monte_carlo",
        }

    def test_a_section_error_carries_its_detail(self) -> None:
        gate = evaluate_validation_gate(
            _passing(monte_carlo={"error": "need at least 3 trades"}),
            config={"source": "local"},
        )

        assert gate.reasons == ["monte_carlo_failed"]
        assert gate.checks["monte_carlo"]["detail"] == "need at least 3 trades"

    @pytest.mark.parametrize("observed", [None, "abc", float("nan"), float("inf"), True])
    def test_an_unusable_metric_fails_the_section(self, observed: object) -> None:
        gate = evaluate_validation_gate(
            _passing(monte_carlo={"p_value_sharpe": observed}),
            config={"source": "local"},
        )

        assert "monte_carlo_failed" in gate.reasons
        assert gate.checks["monte_carlo"]["metric"] == "p_value_sharpe"

    def test_boundary_value_passes_and_is_recorded(self) -> None:
        """p == threshold passes (≤); the check records both numbers."""
        rules = ValidationThresholds(max_p_value_sharpe=0.10)
        gate = evaluate_validation_gate(
            _passing(monte_carlo={"p_value_sharpe": 0.10}),
            config={"source": "local"},
            thresholds=rules,
        )

        assert gate.passed is True
        assert gate.checks["monte_carlo"]["observed"] == 0.10
        assert gate.checks["monte_carlo"]["threshold"] == 0.10

    def test_failed_section_reports_observed_and_threshold(self) -> None:
        gate = evaluate_validation_gate(
            _passing(bootstrap={"ci_lower": -0.4}),
            config={"source": "local"},
        )

        check = gate.checks["bootstrap"]
        assert check["reason"] == "bootstrap_ci_lower"
        assert check["observed"] == -0.4
        assert check["threshold"] == 0.0


# ---------------------------------------------------------------------------
# Reading a run directory
# ---------------------------------------------------------------------------


class TestLoadRunValidation:
    def test_missing_artifact_is_not_a_load_error(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(json.dumps({"source": "local"}), encoding="utf-8")

        results, config, error = load_run_validation(tmp_path)

        assert results is None
        assert config == {"source": "local"}
        assert error is None

    def test_successful_read_returns_both_documents(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (tmp_path / "config.json").write_text('{"source": "local"}', encoding="utf-8")
        (artifacts / "validation.json").write_text('{"cost_aware": true}', encoding="utf-8")

        results, config, error = load_run_validation(tmp_path)

        assert results == {"cost_aware": True}
        assert config == {"source": "local"}
        assert error is None

    def test_unparsable_config_is_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

        assert load_run_validation(tmp_path) == (None, {}, "config_unreadable")

    def test_config_that_is_not_an_object_is_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("[1, 2]", encoding="utf-8")

        assert load_run_validation(tmp_path) == (None, {}, "config_unreadable")

    def test_unparsable_artifact_is_flagged(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "validation.json").write_text("{truncated", encoding="utf-8")

        assert load_run_validation(tmp_path) == (None, {}, "validation_unreadable")

    def test_artifact_that_is_not_an_object_is_flagged(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "validation.json").write_text('"passing"', encoding="utf-8")

        assert load_run_validation(tmp_path) == (None, {}, "validation_unreadable")

    def test_evaluate_run_validation_denies_a_directory_without_an_artifact(
        self, tmp_path: Path
    ) -> None:
        gate = evaluate_run_validation(tmp_path)

        assert gate.passed is False
        assert gate.reasons == ["missing_validation"]

    def test_evaluate_run_validation_denies_an_empty_artifact(self, tmp_path: Path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir()
        (artifacts / "validation.json").write_text("", encoding="utf-8")

        gate = evaluate_run_validation(tmp_path)

        assert gate.reasons == ["validation_unreadable"]
