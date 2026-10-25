"""CSI300 bench must reuse the shared Tushare loader cache across retries."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.tools import alpha_bench_tool as abt


def _raw_daily(code: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": [code, code],
            "trade_date": ["20240102", "20240103"],
            "open": [10.0, 10.5],
            "high": [11.0, 11.5],
            "low": [9.5, 10.0],
            "close": [10.5, 11.0],
            "vol": [1000.0, 1200.0],
            "amount": [10500.0, 13200.0],
        }
    )


def _normalized(code: str) -> pd.DataFrame:
    frame = _raw_daily(code)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date").rename(columns={"vol": "volume"})
    keep = ["open", "high", "low", "close", "volume", "amount"]
    for col in keep:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame[keep]


@pytest.fixture
def fake_duckdb(monkeypatch):
    """DuckDB stand-in so parquet cache write/read works without the package."""

    class _Connection:
        def __init__(self):
            self._tables = {}
            self._frame = None

        def register(self, name, frame):
            self._tables[name] = frame.copy()

        def execute(self, sql):
            start = sql.index("'") + 1
            end = sql.index("'", start)
            path = sql[start:end].replace("''", "'")
            if sql.strip().upper().startswith("COPY "):
                self._tables["cache_frame"].to_pickle(path)
            else:
                self._frame = pd.read_pickle(path)
            return self

        def fetchdf(self):
            return self._frame.copy()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "duckdb",
        SimpleNamespace(connect=lambda database=":memory:": _Connection()),
    )


@pytest.fixture
def csi300_env(monkeypatch, tmp_path, fake_duckdb):
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-test-token")
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "1")
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE_ROOT", str(tmp_path / "loaders"))
    from src.config.accessor import reset_env_config

    reset_env_config()


def test_csi300_panel_uses_pooled_loader_and_keeps_amount(monkeypatch, csi300_env):
    """Cold CSI300 fetch goes through DataLoader; amount survives for CN VWAP."""
    import backtest.loaders.tushare as ts_loader
    import backtest.loaders.tushare_pool as pool_mod

    codes = ["600519.SH", "000001.SZ"]
    weights = pd.DataFrame(
        {"trade_date": ["20240131", "20240131"], "con_code": codes}
    )
    pool = MagicMock()
    pool.call.return_value = weights
    monkeypatch.setattr(pool_mod, "resolve_tushare_tokens", lambda *a, **k: ["unit-test-token"])
    monkeypatch.setattr(pool_mod, "get_tushare_pool", lambda: pool)

    api = MagicMock()

    def _daily(**kwargs):
        joined = kwargs.get("ts_code") or ""
        parts = [p for p in joined.split(",") if p]
        if len(parts) <= 1:
            code = parts[0] if parts else "600519.SH"
            return _raw_daily(code)
        return pd.concat([_raw_daily(code) for code in parts], ignore_index=True)

    api.daily.side_effect = _daily

    loader = ts_loader.DataLoader.__new__(ts_loader.DataLoader)
    loader.api = api
    monkeypatch.setattr(ts_loader, "DataLoader", lambda: loader)

    panel = abt._load_csi300_panel("2024-01-01", "2024-01-31")

    assert "close" in panel
    assert "amount" in panel
    assert "vwap" in panel
    assert set(panel["close"].columns) == set(codes)
    assert api.daily.called
    # Constituents also go through the shared pool (RPM-aware).
    pool.call.assert_called()
    assert pool.call.call_args.args[0] == "index_weight"


def test_csi300_second_load_skips_daily_when_symbols_cached(monkeypatch, csi300_env):
    """A retry after a rate-limit must reuse per-symbol parquet hits."""
    from backtest.loaders.base import loader_cache_forced, loader_cache_put
    import backtest.loaders.tushare as ts_loader
    import backtest.loaders.tushare_pool as pool_mod

    codes = ["600519.SH", "000001.SZ"]
    for code in codes:
        loader_cache_put(
            source="tushare",
            symbol=code,
            timeframe="1D",
            start_date="2024-01-01",
            end_date="2024-01-31",
            fields=[],
            frame=_normalized(code),
        )

    weights = pd.DataFrame(
        {"trade_date": ["20240131", "20240131"], "con_code": codes}
    )
    pool = MagicMock()
    pool.call.return_value = weights
    monkeypatch.setattr(pool_mod, "resolve_tushare_tokens", lambda *a, **k: ["unit-test-token"])
    monkeypatch.setattr(pool_mod, "get_tushare_pool", lambda: pool)

    api = MagicMock()
    api.daily.side_effect = AssertionError("cache hit must not call daily")

    loader = ts_loader.DataLoader.__new__(ts_loader.DataLoader)
    loader.api = api
    monkeypatch.setattr(ts_loader, "DataLoader", lambda: loader)

    with loader_cache_forced(True):
        panel = abt._load_csi300_panel("2024-01-01", "2024-01-31")

    assert set(panel["close"].columns) == set(codes)
    assert "amount" in panel
    api.daily.assert_not_called()
