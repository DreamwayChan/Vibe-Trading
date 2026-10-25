"""Regression coverage for the dedicated Binance crypto fallback."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.loaders.registry import FALLBACK_CHAINS
from backtest.loaders.source_health import reset_for_tests
from src import market_data


@pytest.fixture(autouse=True)
def _reset_host_health() -> None:
    # prefer_order is process-global; clear leftovers from earlier tests so the
    # static crypto chain order (okx → binance) is what this file asserts.
    reset_for_tests()
    yield
    reset_for_tests()


def test_market_data_falls_back_from_okx_to_binance() -> None:
    calls: list[str] = []

    def resolver(source: str):
        calls.append(source)

        class FailedLoader:
            def fetch(self, *_args, **_kwargs):
                raise RuntimeError(f"{source} unavailable")

        class BinanceLoader:
            def fetch(self, codes, *_args, **_kwargs):
                frame = pd.DataFrame(
                    {"close": [1.0]}, index=pd.to_datetime(["2026-01-01"])
                )
                frame.index.name = "trade_date"
                return {codes[0]: frame}

        return BinanceLoader if source == "binance" else FailedLoader

    result = market_data.fetch_market_data(
        codes=["BTC-USDT"],
        start_date="2026-01-01",
        end_date="2026-01-02",
        source="okx",
        loader_resolver=resolver,
        fallback_chain_provider=lambda _source: FALLBACK_CHAINS["crypto"],
    )

    assert calls[:2] == ["okx", "binance"]
    assert "BTC-USDT" in result
