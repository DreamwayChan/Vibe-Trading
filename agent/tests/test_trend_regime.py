"""Long-only 3-state trend regime: bearish prior until CSI 300 is a full bull."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.monitor.trend_regime import (
    classify_structure,
    classify_swing,
    long_only_plan,
    plan_from_frames,
    sort_dicts_by_trend_action,
    stamp_holding_trends,
    summarize_panel,
)


def _ramp(start: float, n: int, step: float) -> pd.Series:
    return pd.Series([start + i * step for i in range(n)])


def test_bull_stack_needs_positive_120d_momentum() -> None:
    # Steady grind higher: MA20>MA60>MA120, close>MA60, 120d > 0.
    close = _ramp(10.0, 200, 0.05)
    snap = classify_structure(close)
    assert snap is not None
    assert snap.regime == "bull"
    assert snap.mom120 > 0


def test_bear_stack_is_the_mirror() -> None:
    close = _ramp(30.0, 200, -0.05)
    snap = classify_structure(close)
    assert snap is not None
    assert snap.regime == "bear"
    assert snap.mom120 < 0


def test_horizons_split_short_medium_long() -> None:
    """A rise then a medium pullback can be medium-bear while still above the year line."""
    from src.monitor.trend_regime import classify_horizons

    climb = [5.0 + i * 0.25 for i in range(160)]
    fade = [climb[-1] - i * 0.05 for i in range(1, 121)]
    close = pd.Series(climb + fade)
    snap = classify_horizons(close)
    assert snap is not None
    assert snap.medium == "bear"
    assert snap.long == "chop"
    assert snap.ma250 is not None
    assert snap.close > snap.ma250
    assert "不是熊市" in snap.flip or "年线" in snap.flip


def test_horizons_all_bear_on_a_grind_down() -> None:
    from src.monitor.trend_regime import classify_horizons

    close = _ramp(80.0, 260, -0.12)
    snap = classify_horizons(close)
    assert snap is not None
    assert snap.short == "bear"
    assert snap.medium == "bear"
    assert snap.long == "bear"


def test_chop_when_mas_overlap() -> None:
    close = pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200)))
    snap = classify_structure(close)
    assert snap is not None
    assert snap.regime == "chop"


def test_swing_reads_range_location() -> None:
    close = pd.Series([10.0] * 50 + [8.0] * 10)
    assert classify_swing(close) == "oversold"
    assert classify_swing(pd.Series([10.0] * 50 + [12.0] * 10)) == "overbought"


def test_index_bull_keeps_fade_on_this_book() -> None:
    """CSI 300 bull did not lift these names — do not void the bearish prior."""
    market = classify_structure(_ramp(10.0, 200, 0.05))
    stock = classify_structure(_ramp(10.0, 200, 0.04))
    assert market is not None and stock is not None
    plan = long_only_plan(market, stock, "mid")
    assert plan.fade_rallies is True
    assert plan.action == "wait"
    stretched = long_only_plan(market, stock, "overbought")
    assert stretched.action == "sell_rally"


def test_index_chop_holds_relative_strength_bull() -> None:
    """A stock bull stack while the index is chop continued in the walk-forward."""
    market = classify_structure(pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200))))
    stock = classify_structure(_ramp(10.0, 200, 0.05))
    assert market is not None and market.regime == "chop"
    assert stock is not None and stock.regime == "bull"
    plan = long_only_plan(market, stock, "mid")
    assert plan.fade_rallies is False
    assert plan.action == "hold"


def test_index_chop_buys_dips_and_sells_range_highs() -> None:
    market = classify_structure(pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200))))
    stock = classify_structure(pd.Series(20.0 + np.sin(np.linspace(0, 10 * np.pi, 200))))
    assert market is not None and stock is not None
    assert market.regime == "chop" and stock.regime == "chop"
    assert long_only_plan(market, stock, "oversold").action == "buy_dip"
    assert long_only_plan(market, stock, "overbought").action == "sell_rally"
    assert long_only_plan(market, stock, "mid").action == "wait"


def test_index_bear_never_opens_a_long() -> None:
    market = classify_structure(_ramp(30.0, 200, -0.05))
    stock = classify_structure(_ramp(20.0, 200, -0.03))
    assert market is not None and market.regime == "bear"
    plan = long_only_plan(market, stock, "oversold")
    assert plan.action == "wait"
    bounce = long_only_plan(market, stock, "overbought")
    assert bounce.action == "sell_rally"


def test_index_chop_bear_reduces() -> None:
    market = classify_structure(pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200))))
    stock = classify_structure(_ramp(30.0, 200, -0.05))
    assert market is not None and stock is not None
    assert market.regime == "chop" and stock.regime == "bear"
    plan = long_only_plan(market, stock, "mid")
    assert plan.action == "reduce"
    assert plan.fade_rallies is True


def test_plan_from_frames_needs_history() -> None:
    short = pd.Series([1.0, 1.1, 1.2])
    assert plan_from_frames(short, short) is None


def test_summarize_panel_counts_hold_and_reduce() -> None:
    idx = pd.RangeIndex(200)
    market = pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200)), index=idx)
    rs = pd.Series([10.0 + i * 0.05 for i in range(200)], index=idx)
    knife = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    summary = summarize_panel(pd.DataFrame({"RS.SZ": rs, "KN.SZ": knife}), market)
    assert summary.classified == 2
    assert summary.rs_n == 1
    assert summary.knife_n == 1
    assert summary.rs_share == pytest.approx(0.5)
    assert summary.knife_share == pytest.approx(0.5)


def test_summarize_panel_keeps_rs_knife_when_index_is_bear() -> None:
    """Index bear rewrites actions to wait/sell; the observation map still uses the stock stack."""
    idx = pd.RangeIndex(200)
    market = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    rs = pd.Series([10.0 + i * 0.05 for i in range(200)], index=idx)
    knife = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    market_snap = classify_structure(market)
    assert market_snap is not None
    assert market_snap.regime == "bear"
    summary = summarize_panel(pd.DataFrame({"RS.SZ": rs, "KN.SZ": knife}), market)
    assert summary.classified == 2
    assert summary.rs_n == 1
    assert summary.knife_n == 1
    assert summary.rs_share == pytest.approx(0.5)
    assert summary.knife_share == pytest.approx(0.5)
    # Trading actions under index bear are wait / sell_rally, never hold / reduce.
    assert summary.wait_n + summary.sell_n == summary.classified


def test_sort_dicts_by_trend_action_puts_hold_first() -> None:
    items = [
        {"symbol": "AAA.SZ", "weight": 0.4, "trend": {"action": "reduce"}},
        {"symbol": "BBB.SZ", "weight": 0.1, "trend": {"action": "hold"}},
        {"symbol": "CCC.SZ", "weight": 0.3, "trend": {"action": "wait"}},
        {"symbol": "DDD.SZ", "weight": 0.2},
    ]
    ordered = [row["symbol"] for row in sort_dicts_by_trend_action(items)]
    assert ordered == ["BBB.SZ", "CCC.SZ", "AAA.SZ", "DDD.SZ"]


def test_stamp_holding_trends_marks_rs_hold_and_sorts() -> None:
    """Index chop + stock bull stack is the relative-strength hold cell."""
    from src.monitor.models import HoldingRow

    idx = pd.RangeIndex(200)
    market = pd.Series(20.0 + np.sin(np.linspace(0, 12 * np.pi, 200)), index=idx)
    rs = pd.Series([10.0 + i * 0.05 for i in range(200)], index=idx)
    knife = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    data_map = {
        "RS.SZ": pd.DataFrame({"close": rs}, index=idx),
        "KN.SZ": pd.DataFrame({"close": knife}, index=idx),
    }
    rows = [
        HoldingRow(symbol="KN.SZ", weight=0.4, action="hold"),
        HoldingRow(symbol="RS.SZ", weight=0.1, action="enter"),
    ]
    stamped = stamp_holding_trends(rows, data_map, market)
    assert [row.symbol for row in stamped] == ["RS.SZ", "KN.SZ"]
    by_symbol = {row.symbol: row.trend for row in stamped}
    assert by_symbol["RS.SZ"] is not None and by_symbol["RS.SZ"]["action"] == "hold"
    assert by_symbol["RS.SZ"]["stock_regime"] == "bull"
    assert by_symbol["KN.SZ"] is not None and by_symbol["KN.SZ"]["action"] == "reduce"
    # enter/trim is a separate axis — trend must not overwrite it.
    assert stamped[0].action == "enter"
