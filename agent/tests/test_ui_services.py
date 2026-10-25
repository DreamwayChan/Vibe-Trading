"""Tests for UI-oriented run reconstruction services."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import backtest.runner as runner
from backtest.loaders.base import NoAvailableSourceError
from src.ui_services import reconstruct_price_series, build_trade_markers


def test_build_trade_markers_skips_end_of_backtest() -> None:
    """Accounting closes at the last bar must not become chart sell markers."""
    trades = [
        {
            "timestamp": "2026-07-21",
            "code": "603387.SH",
            "side": "buy",
            "price": 8.8,
            "qty": 100,
            "reason": "signal",
        },
        {
            "timestamp": "2026-07-24",
            "code": "603387.SH",
            "side": "sell",
            "price": 8.3,
            "qty": 100,
            "reason": "end_of_backtest",
        },
        {
            "timestamp": "2026-06-16",
            "code": "300009.SZ",
            "side": "sell",
            "price": 7.8,
            "qty": 50,
            "reason": "signal",
        },
    ]

    markers = build_trade_markers(trades)

    assert len(markers) == 2
    assert all(m.get("reason") != "end_of_backtest" for m in markers)
    assert {m["code"] for m in markers} == {"603387.SH", "300009.SZ"}
    assert {m["side"] for m in markers} == {"BUY", "SELL"}



@pytest.mark.parametrize("source", ["yahoo", "auto"])
def test_reconstruct_price_series_uses_central_fetch_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    run_dir = tmp_path / "completed-run"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n    pass\n", encoding="utf-8"
    )
    (run_dir / "req.json").write_text(
        json.dumps(
            {
                "prompt": "test",
                "context": {
                    "codes": ["BTC-USDT"],
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-02",
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["BTC-USDT"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "source": source,
                "interval": "1H",
            }
        ),
        encoding="utf-8",
    )

    routed_configs: list[dict] = []
    frame = pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    def fake_fetch_data_map(config: dict) -> SimpleNamespace:
        routed_configs.append(config)
        return SimpleNamespace(data_map={"BTC-USDT": frame})

    monkeypatch.setattr(runner, "fetch_data_map", fake_fetch_data_map)

    rows = reconstruct_price_series(run_dir)

    assert routed_configs[0]["source"] == source
    assert routed_configs[0]["interval"] == "1H"
    assert routed_configs[0]["codes"] == ["BTC-USDT"]
    assert rows[0]["code"] == "BTC-USDT"


def test_fetch_data_map_uses_registry_for_nonlegacy_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    class StubLoader:
        name = "yahoo"

        def fetch(self, codes, start_date, end_date, **kwargs):
            calls.append((codes, start_date, end_date, kwargs))
            return {codes[0]: frame}

    monkeypatch.setattr(runner, "_get_loader", lambda source: StubLoader)
    config = {
        "codes": ["AAPL.US"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
        "source": "yahoo",
        "interval": "1H",
    }
    original = dict(config)

    result = runner.fetch_data_map(config)

    assert config == original
    assert calls == [
        (
            ["AAPL.US"],
            "2026-01-01",
            "2026-01-02",
            {"fields": None, "interval": "1H"},
        )
    ]
    assert result.source == "yahoo"
    assert result.effective_sources == ["yahoo"]
    assert list(result.data_map) == ["AAPL.US"]


def test_fetch_data_map_does_not_expose_config_mutables_to_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    class MutatingLoader:
        name = "tushare"

        def fetch(self, codes, start_date, end_date, **kwargs):
            kwargs["fields"].append("injected")
            return {codes[0]: frame}

    monkeypatch.setattr(runner, "_get_loader", lambda source: MutatingLoader)
    config = {
        "codes": ["000001.SZ"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
        "source": "tushare",
        "extra_fields": ["amount"],
    }

    runner.fetch_data_map(config)

    assert config["extra_fields"] == ["amount"]


def test_fetch_data_map_delegates_auto_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )
    calls: list[tuple[list[str], dict, str]] = []

    def fake_fetch_auto(codes: list[str], config: dict, interval: str, **_kwargs) -> dict:
        calls.append((codes, config, interval))
        return {"AAPL.US": frame}

    monkeypatch.setattr(runner, "_fetch_auto", fake_fetch_auto)
    config = {
        "codes": ["AAPL.US"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
        "source": "auto",
        "interval": "1D",
    }

    result = runner.fetch_data_map(config)

    assert calls == [(["AAPL.US"], config, "1D")]
    assert result.source == "auto"
    assert result.effective_sources == ["yfinance"]


def test_main_reuses_explicit_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n    pass\n", encoding="utf-8"
    )
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["AAPL.US"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "source": "yahoo",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))

    first = pd.DataFrame(
        {"open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )
    second = first.assign(open=20.0, high=20.0, low=20.0, close=20.0)

    class CountingLoader:
        name = "yahoo"
        calls = 0

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            type(self).calls += 1
            frame = first if type(self).calls == 1 else second
            return {codes[0]: frame}

    observed: dict[str, float] = {}

    class CapturingEngine:
        def run_backtest(self, config, loader, signal_engine, path, **kwargs):
            del signal_engine, path, kwargs
            data = loader.fetch(
                config["codes"], config["start_date"], config["end_date"]
            )
            observed["close"] = float(data["AAPL.US"]["close"].iloc[0])

    monkeypatch.setattr(runner, "_get_loader", lambda source: CountingLoader)
    monkeypatch.setattr(
        runner,
        "_load_module_from_file",
        lambda path, name: SimpleNamespace(SignalEngine=type("SignalEngine", (), {})),
    )
    monkeypatch.setattr(runner, "_validate_signal_engine_class", lambda cls: None)
    monkeypatch.setattr(
        runner, "_create_market_engine", lambda source, config, codes: CapturingEngine()
    )

    runner.main(run_dir)

    assert CountingLoader.calls == 1
    assert observed["close"] == 10.0


def test_fetch_auto_restores_original_crypto_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    class OkxLoader:
        name = "okx"

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            assert codes == ["BTC-USDT"]
            return {"BTC-USDT": frame}

    monkeypatch.setattr(runner, "resolve_loader", lambda market: OkxLoader())

    result = runner._fetch_auto(
        ["BTC/USDT"],
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
    )

    assert list(result) == ["BTC/USDT"]


def test_fetch_auto_falls_back_only_for_missing_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )
    calls: list[tuple[str, list[str]]] = []

    class PrimaryLoader:
        name = "primary"

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            calls.append((self.name, list(codes)))
            return {"AAPL.US": frame}

    class BackupLoader:
        name = "backup"

        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            calls.append((self.name, list(codes)))
            return {code: frame for code in codes}

    monkeypatch.setattr(runner, "resolve_loader", lambda market: PrimaryLoader())
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "us_equity", ["primary", "backup"])
    monkeypatch.setitem(runner.LOADER_REGISTRY, "backup", BackupLoader)

    result = runner._fetch_auto(
        ["AAPL.US", "MSFT.US"],
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
    )

    assert list(result) == ["AAPL.US", "MSFT.US"]
    assert calls == [
        ("primary", ["AAPL.US", "MSFT.US"]),
        ("backup", ["MSFT.US"]),
    ]


def test_explicit_fetch_falls_back_only_for_missing_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )
    calls: list[tuple[str, list[str]]] = []

    class PrimaryLoader:
        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            calls.append(("primary", list(codes)))
            return {"AAPL.US": frame}

    class BackupLoader:
        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            calls.append(("backup", list(codes)))
            return {code: frame for code in codes}

    monkeypatch.setattr(runner, "_get_loader", lambda source: PrimaryLoader)
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "us_equity", ["backup"])
    monkeypatch.setitem(runner.LOADER_REGISTRY, "backup", BackupLoader)

    result = runner.fetch_data_map(
        {
            "codes": ["AAPL.US", "MSFT.US"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "source": "primary",
        }
    )

    assert list(result.data_map) == ["AAPL.US", "MSFT.US"]
    assert result.effective_sources == ["primary", "backup"]
    assert calls == [
        ("primary", ["AAPL.US", "MSFT.US"]),
        ("backup", ["MSFT.US"]),
    ]


def test_fetch_stops_when_fallbacks_leave_symbols_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    class PartialLoader:
        name = "primary"

        def fetch(self, codes, start_date, end_date, **kwargs):
            del codes, start_date, end_date, kwargs
            return {"AAPL.US": frame}

    config = {"start_date": "2026-01-01", "end_date": "2026-01-02"}
    monkeypatch.setattr(runner, "resolve_loader", lambda market: PartialLoader())
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "us_equity", [])

    with pytest.raises(NoAvailableSourceError, match="MSFT.US"):
        runner._fetch_auto(["AAPL.US", "MSFT.US"], config)

    monkeypatch.setattr(runner, "_get_loader", lambda source: PartialLoader)
    with pytest.raises(NoAvailableSourceError, match="MSFT.US"):
        runner.fetch_data_map(
            {
                **config,
                "codes": ["AAPL.US", "MSFT.US"],
                "source": "primary",
            }
        )


def test_fetch_data_map_allow_partial_keeps_successful_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )

    class PartialLoader:
        name = "primary"

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            return {codes[0]: frame}

    monkeypatch.setattr(runner, "_get_loader", lambda source: PartialLoader)
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "us_equity", [])

    result = runner.fetch_data_map(
        {
            "codes": ["AAPL.US", "MSFT.US"],
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "source": "primary",
        },
        allow_partial=True,
    )
    assert list(result.data_map) == ["AAPL.US"]


def test_fetch_auto_large_a_share_uses_tushare_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")]),
    )
    fetched: list[list[str]] = []

    class TushareLoader:
        name = "tushare"

        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, **kwargs):
            del start_date, end_date, kwargs
            fetched.append(list(codes))
            return {code: frame for code in codes}

    monkeypatch.setitem(runner.LOADER_REGISTRY, "tushare", TushareLoader)
    monkeypatch.setattr(runner, "is_cooling_down", lambda _name: False)
    monkeypatch.setattr(
        runner,
        "resolve_loader",
        lambda market: (_ for _ in ()).throw(AssertionError("public chain")),
    )
    codes = [f"{i:06d}.SZ" for i in range(1, 45)]
    result = runner._fetch_auto(
        codes,
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        allow_partial=True,
    )
    assert fetched and len(fetched[0]) == 44
    assert len(result) == 44


def test_fetch_auto_skips_public_hosts_when_many_names_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TushareLoader:
        name = "tushare"

        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, **kwargs):
            del codes, start_date, end_date, kwargs
            return {}

    class TencentLoader:
        name = "tencent"

        def is_available(self):
            return True

        def fetch(self, codes, start_date, end_date, **kwargs):
            del codes, start_date, end_date, kwargs
            raise AssertionError("must not walk tencent for a large leftover set")

    monkeypatch.setitem(runner.LOADER_REGISTRY, "tushare", TushareLoader)
    monkeypatch.setitem(runner.LOADER_REGISTRY, "tencent", TencentLoader)
    monkeypatch.setattr(runner, "is_cooling_down", lambda _name: False)
    monkeypatch.setitem(runner.FALLBACK_CHAINS, "a_share", ["tushare", "tencent"])
    codes = [f"{i:06d}.SZ" for i in range(1, 45)]
    result = runner._fetch_auto(
        codes,
        {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        allow_partial=True,
    )
    assert result == {}
