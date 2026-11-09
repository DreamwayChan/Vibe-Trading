"""Edge paths for the bound-backtest expectation, without a full paper session."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.paper_book.expectation import (
    choose_anchor,
    expectation_metrics,
    load_equity,
    slippage_gap,
)


def test_load_equity_missing_or_unusable(tmp_path: Path) -> None:
    assert load_equity(None) is None
    assert load_equity(tmp_path) is None
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "equity.csv").write_text("timestamp,other\n2026-01-05,1\n", encoding="utf-8")
    assert load_equity(tmp_path) is None

    (artifacts / "equity.csv").write_text(
        "date,nav\n2026-01-05,0\n2026-01-06,-1\n",
        encoding="utf-8",
    )
    assert load_equity(tmp_path) is None

    (artifacts / "equity.csv").write_text(
        "trade_date,value\n2026-01-05,10\n2026-01-05,12\n2026-01-06,not-a-number\n2026-01-07,11\n",
        encoding="utf-8",
    )
    series = load_equity(tmp_path)
    assert series is not None
    assert list(series.index) == ["2026-01-05", "2026-01-07"]
    assert float(series.iloc[0]) == 12.0


def test_load_equity_reads_a_zoned_timestamp(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "equity.csv").write_text(
        "timestamp,equity\n2026-01-05T16:00:00+08:00,100\n",
        encoding="utf-8",
    )
    series = load_equity(tmp_path)
    assert series is not None
    assert series.index[0] == "2026-01-05"


def test_choose_anchor_prefers_the_prior_mark() -> None:
    equity = pd.Series([100.0, 110.0], index=["2026-01-02", "2026-01-05"])
    assert choose_anchor(equity, "2026-01-05") == ("2026-01-02", 100.0)
    assert choose_anchor(pd.Series(dtype=float), "2026-01-05") is None
    same_day = pd.Series([110.0], index=["2026-01-05"])
    assert choose_anchor(same_day, "2026-01-05") == ("2026-01-05", 110.0)
    later = pd.Series([110.0], index=["2026-01-07"])
    assert choose_anchor(later, "2026-01-06") is None


def test_expectation_metrics_scales_the_gap() -> None:
    missing = expectation_metrics(
        None,
        as_of="2026-01-05",
        anchor_date=None,
        anchor_equity=None,
        initial_cash=1_000_000.0,
        paper_cum_return=0.0,
        paper_daily_return=0.0,
    )
    assert missing["available"] is False
    assert missing["daily_return_gap"] is None

    equity = pd.Series([100.0, 110.0], index=["2026-01-02", "2026-01-05"])
    ready = expectation_metrics(
        equity,
        as_of="2026-01-05",
        anchor_date="2026-01-02",
        anchor_equity=100.0,
        initial_cash=1_000.0,
        paper_cum_return=0.05,
        paper_daily_return=0.01,
    )
    assert ready["available"] is True
    assert ready["expected_cum_return"] == pytest.approx(0.1)
    assert ready["expected_nav"] == pytest.approx(1_100.0)
    assert ready["return_deviation"] == pytest.approx(0.05)
    assert ready["expected_daily_return"] == pytest.approx(0.1)
    assert ready["daily_return_gap"] == pytest.approx(-0.09)

    flat = expectation_metrics(
        pd.Series([100.0], index=["2026-01-05"]),
        as_of="2026-01-05",
        anchor_date="2026-01-05",
        anchor_equity=100.0,
        initial_cash=1_000.0,
        paper_cum_return=0.0,
        paper_daily_return=0.02,
    )
    assert flat["expected_daily_return"] == 0.0
    assert flat["daily_return_gap"] == 0.02


def test_slippage_gap_needs_a_matching_trade(tmp_path: Path) -> None:
    assert slippage_gap(None, "2026-01-05", [{"side": "buy"}]) is None
    assert slippage_gap(tmp_path, "2026-01-05", []) is None
    assert slippage_gap(tmp_path, "2026-01-05", [{"side": "buy", "symbol": "AAA", "fill_price": 1}]) is None

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "trades.csv").write_text("timestamp,code\n2026-01-05,AAA\n", encoding="utf-8")
    assert slippage_gap(tmp_path, "2026-01-05", [{"side": "buy", "symbol": "AAA", "fill_price": 1}]) is None

    (artifacts / "trades.csv").write_text(
        "timestamp,code,side,price\n"
        "2026-01-06,AAA,buy,10\n"
        "2026-01-05,AAA,buy,0\n"
        "2026-01-05,BBB,buy,10\n"
        "2026-01-05,AAA,sell,10\n",
        encoding="utf-8",
    )
    fills = [
        {"side": "buy", "symbol": "aaa", "fill_price": "nope"},
        {"side": "buy", "symbol": "AAA", "fill_price": 11.0},
    ]
    assert slippage_gap(tmp_path, "2026-01-05", fills) is None

    (artifacts / "trades.csv").write_text(
        "timestamp,code,side,price\n"
        "2026-01-05,AAA,buy,10\n"
        "2026-01-05,AAA,buy,12\n"
        "2026-01-05,BBB,buy,8\n",
        encoding="utf-8",
    )
    gap = slippage_gap(
        tmp_path,
        "2026-01-05",
        [
            {"side": "buy", "symbol": "AAA", "fill_price": 12.1},
            {"side": "buy", "symbol": "BBB", "fill_price": 8.8},
        ],
    )
    assert gap is not None
    # AAA backtest mean is 11, gap (12.1-11)/11; BBB (8.8-8)/8.
    assert gap == ((12.1 - 11.0) / 11.0 + (8.8 - 8.0) / 8.0) / 2
