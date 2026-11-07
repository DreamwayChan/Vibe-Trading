"""Shared A-share cost and fill defaults for the runner and the monitor.

The monitor re-bench calls ``_create_market_engine``, so these tests cover the
path both entry points use.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from backtest.models import Position
from backtest.runner import _create_market_engine


def _bar(**extra) -> pd.Series:
    data = {"close": 15.0, "open": 15.0, "trade_date": pd.Timestamp("2025-06-10")}
    data.update(extra)
    return pd.Series(data)


def test_runner_a_share_defaults_match_the_retail_schedule() -> None:
    engine = _create_market_engine("tushare", {"initial_cash": 1_000_000}, ["600519.SH"])
    assert isinstance(engine, ChinaAEngine)
    assert engine.commission_rate == pytest.approx(0.00025)
    assert engine.commission_min == pytest.approx(5.0)
    assert engine.stamp_tax == pytest.approx(0.0005)
    assert engine.transfer_fee == pytest.approx(0.00001)
    assert engine.slippage_rate == pytest.approx(0.001)
    assert engine.t_plus_one is True
    assert engine.block_suspension is True
    assert engine.enforce_price_limits is True


def test_config_overrides_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_ASHARE_SLIPPAGE", "0.02")
    monkeypatch.setenv("VIBE_TRADING_ASHARE_STAMP_TAX", "0.001")
    reset_env_config()

    engine = _create_market_engine(
        "lake",
        {"slippage": 0.0, "initial_cash": 1_000_000},
        ["000001.SZ"],
    )
    assert isinstance(engine, ChinaAEngine)
    assert engine.slippage_rate == pytest.approx(0.0)
    assert engine.stamp_tax == pytest.approx(0.001)
    assert engine.commission_rate == pytest.approx(0.00025)


def test_routed_engine_blocks_same_day_sell_limit_up_and_halt() -> None:
    engine = _create_market_engine("eastmoney", {}, ["300750.SZ"])
    assert isinstance(engine, ChinaAEngine)

    engine.positions["300750.SZ"] = Position(
        symbol="300750.SZ",
        direction=1,
        entry_price=15.0,
        entry_time=pd.Timestamp("2025-06-10"),
        size=100.0,
    )
    assert engine.can_execute("300750.SZ", 0, _bar(volume=1_000.0)) is False

    limit_up = _bar(close=18.0, pre_close=15.0, volume=1_000.0)
    assert engine.can_execute("300750.SZ", 1, limit_up) is False

    halted = _bar(volume=0.0, pre_close=15.0, close=15.1)
    assert engine.can_execute("300750.SZ", 1, halted) is False

    buy = engine.calc_commission(10_000, 15.0, 1, is_open=True)
    sell = engine.calc_commission(10_000, 15.0, 1, is_open=False)
    assert sell - buy == pytest.approx(10_000 * 15.0 * engine.stamp_tax)
    assert engine.apply_slippage(100.0, 1) == pytest.approx(100.1)


def test_tplus1_env_allows_same_day_sell(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_ASHARE_TPLUS1", "0")
    reset_env_config()
    engine = _create_market_engine("tushare", {}, ["000001.SZ"])
    engine.positions["000001.SZ"] = Position(
        symbol="000001.SZ",
        direction=1,
        entry_price=15.0,
        entry_time=pd.Timestamp("2025-06-10"),
        size=100.0,
    )
    assert engine.t_plus_one is False
    assert engine.can_execute("000001.SZ", 0, _bar(volume=1000.0)) is True


def test_composite_a_share_leg_uses_the_same_schedule() -> None:
    from backtest.engines.composite import CompositeEngine

    engine = CompositeEngine({}, ["000001.SZ", "AAPL.US"])
    leg = engine._rule_engines["a_share"]
    assert isinstance(leg, ChinaAEngine)
    assert leg.stamp_tax == pytest.approx(0.0005)
    assert leg.t_plus_one is True
    assert leg.slippage_rate == pytest.approx(0.001)
