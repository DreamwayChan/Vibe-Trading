"""Same strategy, side by side: frictionless vs A-share cost+tradability.

The production default stays cost-aware. These tests run both profiles on
one window and check that fees and refused limit/halt/T+1 orders show up
as a delta.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.ashare_costs import FRICTIONLESS_PROFILE, apply_frictionless
from backtest.cost_compare import compare_profiles, compare_run_dir, main
from backtest.engines.china_a import ChinaAEngine
from backtest.models import Position
from backtest.runner import DataFetchResult, RunPrepError


def _clear_cost_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config.accessor import reset_env_config

    for key in (
        "VIBE_TRADING_ASHARE_COMMISSION_RATE",
        "VIBE_TRADING_ASHARE_COMMISSION_MIN",
        "VIBE_TRADING_ASHARE_STAMP_TAX",
        "VIBE_TRADING_ASHARE_TRANSFER_FEE",
        "VIBE_TRADING_ASHARE_SLIPPAGE",
        "VIBE_TRADING_ASHARE_TPLUS1",
        "VIBE_TRADING_ASHARE_BLOCK_SUSPENSION",
        "VIBE_TRADING_TRADABILITY_PANEL",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_env_config()


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame.index = pd.DatetimeIndex(frame.pop("timestamp"))
    return frame


class _Bars:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple] = []

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        self.calls.append((list(codes), start_date, end_date, interval))
        return {codes[0]: self.frame.copy()}


class _AlwaysLong:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, data_map):
        self.calls += 1
        return {
            code: pd.Series(1.0, index=frame.index)
            for code, frame in data_map.items()
        }


class _BuyThenFlat:
    """Signal 1 on the first bar only, so the shifted target buys on bar 2."""

    def generate(self, data_map):
        out = {}
        for code, frame in data_map.items():
            weights = pd.Series(0.0, index=frame.index)
            weights.iloc[0] = 1.0
            out[code] = weights
        return out


def _strategy(code: str = "000001.SZ") -> dict:
    return {
        "codes": [code],
        "start_date": "2024-03-04",
        "end_date": "2024-03-07",
        "source": "lake",
        "interval": "1D",
        "initial_cash": 1_000_000,
    }


def _assert_delta(report: dict) -> None:
    cost = report["metrics"]["ashare_costs"]
    free = report["metrics"]["frictionless"]
    delta = report["metrics"]["delta"]
    for key, cost_value in cost.items():
        assert delta[key] == pytest.approx(cost_value - free[key])


def _assert_profile_knobs(report: dict) -> None:
    free = report["profiles"]["frictionless"]["knobs"]
    for key, value in FRICTIONLESS_PROFILE.items():
        assert free[key] == value
    assert "无成本" in report["profiles"]["frictionless"]["label_zh"]
    assert "成本+涨跌停" in report["profiles"]["ashare_costs"]["label_zh"]
    assert report["production_default"] == "ashare_costs"
    cost = report["profiles"]["ashare_costs"]["knobs"]
    fresh = ChinaAEngine({})
    assert cost["stamp_tax"] == pytest.approx(fresh.stamp_tax)
    assert cost["slippage"] == pytest.approx(fresh.slippage_rate)
    assert cost["t_plus_one"] is True
    assert cost["enforce_price_limits"] is True
    assert cost["block_suspension"] is True


def test_frictionless_knobs_ignore_limits_halts_and_same_day_sells() -> None:
    panel_row = {
        "symbol": "000001.SZ",
        "is_suspended": True,
        "limit_up": 11.0,
        "limit_down": 9.0,
        "band_mismatch": False,
    }

    class _Panel:
        def get(self, symbol, day):
            return dict(panel_row)

    bar = pd.Series({
        "close": 11.0,
        "open": 11.0,
        "pre_close": 10.0,
        "pct_chg": 10.0,
        "volume": 0.0,
        "trade_date": pd.Timestamp("2024-03-05"),
    })
    free = ChinaAEngine(apply_frictionless({"tradability_index": _Panel()}))
    assert free.tradability_index is not None
    assert free.can_execute("000001.SZ", -1, bar) is False
    assert free.can_execute("000001.SZ", 1, bar) is True
    assert free.round_size(250, 10) == 200
    free.positions["000001.SZ"] = Position(
        symbol="000001.SZ",
        direction=1,
        entry_price=11.0,
        entry_time=pd.Timestamp("2024-03-05"),
        size=100,
    )
    assert free.can_execute("000001.SZ", 0, bar) is True
    assert free.calc_commission(1000, 10.0, 1, is_open=False) == 0.0

    default = ChinaAEngine({"tradability_index": _Panel()})
    assert default.enforce_price_limits is True
    assert default.can_execute("000001.SZ", 1, bar) is False


def test_fee_tax_same_fills_lower_nav(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _clear_cost_env(monkeypatch)
    code = "000001.SZ"
    prices = [10.0, 10.3, 10.6, 10.9]
    frame = _frame([
        {
            "timestamp": day,
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 1_000_000.0,
            "pct_chg": 0.0 if i == 0 else 3.0,
            "pre_close": prices[0] if i == 0 else prices[i - 1],
        }
        for i, (day, price) in enumerate(zip(
            ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07"],
            prices,
        ))
    ])
    loader = _Bars(frame)
    signal = _AlwaysLong()
    report = compare_profiles(_strategy(code), loader, signal, bars_per_year=252)
    captured = capsys.readouterr()
    assert captured.out == ""

    assert signal.calls == 2
    assert loader.calls[0] == loader.calls[1]
    _assert_delta(report)
    _assert_profile_knobs(report)
    cost = report["metrics"]["ashare_costs"]
    free = report["metrics"]["frictionless"]
    assert cost["fill_count"] == free["fill_count"] > 0
    assert cost["unfill_count"] == free["unfill_count"] == 0
    assert cost["trade_count"] == free["trade_count"] > 0
    assert report["metrics"]["delta"]["nav"] < -1
    assert report["metrics"]["delta"]["total_return"] < 0
    assert free["nav"] > cost["nav"]
    # The comparison must not become the production default.
    assert ChinaAEngine({}).stamp_tax == pytest.approx(0.0005)
    assert ChinaAEngine({}).enforce_price_limits is True


def test_limit_up_refuses_only_the_cost_aware_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cost_env(monkeypatch)
    frame = _frame([
        {
            "timestamp": "2024-03-04",
            "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
            "volume": 1_000_000.0, "pct_chg": 0.0, "pre_close": 10.0,
        },
        {
            "timestamp": "2024-03-05",
            "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0,
            "volume": 1_000_000.0, "pct_chg": 10.0, "pre_close": 10.0,
        },
        {
            "timestamp": "2024-03-06",
            "open": 11.2, "high": 11.3, "low": 11.1, "close": 11.2,
            "volume": 1_000_000.0, "pct_chg": 1.8, "pre_close": 11.0,
        },
    ])
    report = compare_profiles(_strategy(), _Bars(frame), _BuyThenFlat(), bars_per_year=252)
    cost = report["metrics"]["ashare_costs"]
    free = report["metrics"]["frictionless"]
    assert cost["trade_count"] == 0
    assert cost["fill_count"] == 0
    assert cost["unfill_count"] >= 1
    assert free["trade_count"] >= 1
    assert free["fill_count"] >= 2
    assert free["unfill_count"] == 0
    assert report["metrics"]["delta"]["unfill_count"] >= 1
    assert report["metrics"]["delta"]["fill_count"] < 0
    assert free["nav"] != pytest.approx(cost["nav"])


def test_same_day_sell_is_refused_only_when_t_plus_one_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cost_env(monkeypatch)
    stamps = [
        "2024-01-02 09:35",
        "2024-01-02 09:40",
        "2024-01-02 09:45",
        "2024-01-02 09:50",
        "2024-01-03 09:35",
    ]
    prices = [10.0, 10.0, 10.0, 10.0, 11.0]
    frame = _frame([
        {
            "timestamp": stamp,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 100_000.0,
            "pct_chg": 0.0 if i == 0 else (0.0 if price == 10.0 else 10.0),
            "pre_close": 10.0,
        }
        for i, (stamp, price) in enumerate(zip(stamps, prices))
    ])
    config = _strategy()
    config["interval"] = "5m"
    config["start_date"] = "2024-01-02"
    config["end_date"] = "2024-01-03"
    report = compare_profiles(config, _Bars(frame), _BuyThenFlat(), bars_per_year=252 * 48)
    cost = report["metrics"]["ashare_costs"]
    free = report["metrics"]["frictionless"]
    assert free["unfill_count"] == 0
    assert cost["unfill_count"] > free["unfill_count"]
    assert report["profiles"]["frictionless"]["knobs"]["t_plus_one"] is False
    assert report["profiles"]["ashare_costs"]["knobs"]["t_plus_one"] is True
    # Frictionless sells the same morning. The cost-aware book is still long
    # into the next session's higher open, so the two NAVs diverge.
    assert free["nav"] != pytest.approx(cost["nav"])


def test_frictionless_profile_ignores_env_slippage(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config.accessor import reset_env_config

    _clear_cost_env(monkeypatch)
    monkeypatch.setenv("VIBE_TRADING_ASHARE_SLIPPAGE", "0.05")
    reset_env_config()
    frame = _frame([
        {
            "timestamp": day,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000_000.0, "pct_chg": 1.0, "pre_close": price,
        }
        for day, price in (
            ("2024-03-04", 10.0),
            ("2024-03-05", 10.2),
            ("2024-03-06", 10.4),
        )
    ])
    report = compare_profiles(_strategy(), _Bars(frame), _AlwaysLong(), bars_per_year=252)
    assert report["profiles"]["frictionless"]["knobs"]["slippage"] == 0.0
    assert report["profiles"]["ashare_costs"]["knobs"]["slippage"] == pytest.approx(0.05)
    assert report["metrics"]["delta"]["nav"] < 0


def test_explicit_config_fees_stay_on_the_cost_leg_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cost_env(monkeypatch)
    config = _strategy()
    config["slippage"] = 0.0
    config["commission_rate"] = 0.0
    config["commission_min"] = 0.0
    config["stamp_tax"] = 0.01
    config["transfer_fee"] = 0.0
    frame = _frame([
        {
            "timestamp": day,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000_000.0, "pct_chg": 1.0, "pre_close": price,
        }
        for day, price in (
            ("2024-03-04", 10.0),
            ("2024-03-05", 10.2),
            ("2024-03-06", 10.4),
        )
    ])
    report = compare_profiles(config, _Bars(frame), _AlwaysLong(), bars_per_year=252)
    assert report["profiles"]["ashare_costs"]["knobs"]["stamp_tax"] == pytest.approx(0.01)
    assert report["profiles"]["frictionless"]["knobs"]["stamp_tax"] == 0.0
    assert report["metrics"]["delta"]["nav"] < 0


_SIGNAL = """
import pandas as pd

class SignalEngine:
    def generate(self, data_map):
        out = {}
        for code, frame in data_map.items():
            out[code] = pd.Series(1.0, index=frame.index)
        return out
"""


def _write_run(root: Path, code: str = "000001.SZ") -> Path:
    run_dir = root / "runs" / "cmp"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "config.json").write_text(json.dumps({
        "codes": [code],
        "start_date": "2024-03-04",
        "end_date": "2024-03-07",
        "source": "lake",
        "interval": "1D",
        "initial_cash": 1_000_000,
    }), encoding="utf-8")
    (run_dir / "code" / "signal_engine.py").write_text(_SIGNAL, encoding="utf-8")
    return run_dir


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame) -> None:
    def fake_fetch(config, allow_partial=False):
        code = config["codes"][0]
        return DataFetchResult(
            data_map={code: frame.copy()},
            codes=list(config["codes"]),
            source=str(config.get("source") or "lake"),
            loader=None,
            effective_sources=["lake"],
        )

    monkeypatch.setattr("backtest.runner.fetch_data_map", fake_fetch)


def _quiet_frame() -> pd.DataFrame:
    prices = [10.0, 10.3, 10.6, 10.9]
    return _frame([
        {
            "timestamp": day,
            "open": price, "high": price, "low": price, "close": price,
            "volume": 1_000_000.0, "pct_chg": 2.0, "pre_close": price,
        }
        for day, price in zip(
            ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-07"],
            prices,
        )
    ])


def test_cli_writes_compare_json_and_leaves_metrics_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.config.accessor import reset_env_config

    _clear_cost_env(monkeypatch)
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "0")
    reset_env_config()
    run_dir = _write_run(tmp_path)
    sentinel = run_dir / "artifacts" / "metrics.csv"
    sentinel.parent.mkdir()
    sentinel.write_text("final_value\n1\n", encoding="utf-8")
    _patch_fetch(monkeypatch, _quiet_frame())

    assert main([str(run_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    saved = json.loads((run_dir / "artifacts" / "cost_compare.json").read_text(encoding="utf-8"))
    assert payload["metrics"]["delta"]["nav"] == saved["metrics"]["delta"]["nav"]
    assert saved["metrics"]["delta"]["nav"] < 0
    assert sentinel.read_text(encoding="utf-8") == "final_value\n1\n"
    assert not (run_dir / "run_card.json").exists()
    assert not (run_dir / "artifacts" / "equity.csv").exists()


def test_cli_rejects_a_non_a_share_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.config.accessor import reset_env_config

    _clear_cost_env(monkeypatch)
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    reset_env_config()
    run_dir = _write_run(tmp_path, code="AAPL.US")
    _patch_fetch(monkeypatch, _quiet_frame())
    assert main([str(run_dir)]) == 1
    body = json.loads(capsys.readouterr().out)
    assert "ChinaAEngine" in body["error"]
    assert not (run_dir / "artifacts" / "cost_compare.json").exists()


def test_vibe_trading_parser_accepts_cost_compare() -> None:
    from cli._legacy import _build_parser

    args = _build_parser().parse_args(["cost-compare", "/tmp/run-a"])
    assert args.command == "cost-compare"
    assert args.run_dir == "/tmp/run-a"


def test_api_cost_compare_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import api_server
    from src.config.accessor import reset_env_config

    _clear_cost_env(monkeypatch)
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(api_server, "RUNS_DIR", runs)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "0")
    reset_env_config()

    run_dir = runs / "cmp"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "config.json").write_text(json.dumps({
        "codes": ["000001.SZ"],
        "start_date": "2024-03-04",
        "end_date": "2024-03-07",
        "source": "lake",
        "interval": "1D",
        "initial_cash": 1_000_000,
    }), encoding="utf-8")
    (run_dir / "code" / "signal_engine.py").write_text(_SIGNAL, encoding="utf-8")
    _patch_fetch(monkeypatch, _quiet_frame())

    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    missing = client.get("/runs/cmp/cost-compare")
    assert missing.status_code == 404

    created = client.post("/runs/cmp/cost-compare")
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["metrics"]["delta"]["nav"] < 0
    assert body["profiles"]["frictionless"]["knobs"]["stamp_tax"] == 0
    assert body["profiles"]["ashare_costs"]["knobs"]["enforce_price_limits"] is True

    fetched = client.get("/runs/cmp/cost-compare")
    assert fetched.status_code == 200
    assert fetched.json()["metrics"]["delta"] == body["metrics"]["delta"]

    detail = client.get("/runs/cmp")
    assert detail.status_code == 200
    assert detail.json()["cost_compare"]["production_default"] == "ashare_costs"
    assert not (run_dir / "artifacts" / "metrics.csv").exists()


def test_compare_run_dir_error_is_a_prep_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    reset_env_config()
    with pytest.raises(RunPrepError, match="config.json not found"):
        compare_run_dir(tmp_path / "missing")
