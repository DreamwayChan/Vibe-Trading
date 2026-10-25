"""Tests for per-symbol attribution and strategy-fit assessment."""

from __future__ import annotations

from src.api.run_report import (
    build_exit_reason_breakdown,
    build_run_report_extras,
    build_strategy_assessment,
    build_symbol_breakdown,
)


def _sample_trades() -> list[dict]:
    """Entry/exit pairs across two symbols with mixed outcomes."""
    return [
        {"timestamp": "2024-01-02", "code": "AAA", "side": "buy", "price": 10, "qty": 100,
         "reason": "signal", "pnl": 0, "holding_days": 0, "return_pct": 0},
        {"timestamp": "2024-01-05", "code": "AAA", "side": "sell", "price": 11, "qty": 100,
         "reason": "signal", "pnl": 100, "holding_days": 3, "return_pct": 10},
        {"timestamp": "2024-01-03", "code": "BBB", "side": "buy", "price": 20, "qty": 50,
         "reason": "signal", "pnl": 0, "holding_days": 0, "return_pct": 0},
        {"timestamp": "2024-01-10", "code": "BBB", "side": "sell", "price": 18, "qty": 50,
         "reason": "end_of_backtest", "pnl": -100, "holding_days": 7, "return_pct": -10},
        {"timestamp": "2024-01-08", "code": "AAA", "side": "buy", "price": 11, "qty": 100,
         "reason": "signal", "pnl": 0, "holding_days": 0, "return_pct": 0},
        {"timestamp": "2024-01-09", "code": "AAA", "side": "sell", "price": 12, "qty": 100,
         "reason": "signal", "pnl": 100, "holding_days": 1, "return_pct": 9},
    ]


def test_symbol_breakdown_ranks_by_total_pnl() -> None:
    rows = build_symbol_breakdown(_sample_trades())
    assert [row["code"] for row in rows] == ["AAA", "BBB"]
    assert rows[0]["total_pnl"] == 200.0
    assert rows[0]["count"] == 2
    assert rows[0]["win_rate"] == 1.0
    assert rows[1]["total_pnl"] == -100.0
    assert rows[0]["contribution_pct"] + rows[1]["contribution_pct"] == 1.0


def test_exit_reason_breakdown_includes_win_rate() -> None:
    rows = build_exit_reason_breakdown(_sample_trades())
    by_reason = {row["reason"]: row for row in rows}
    assert by_reason["signal"]["count"] == 2
    assert by_reason["signal"]["total_pnl"] == 200.0
    assert by_reason["end_of_backtest"]["count"] == 1
    assert by_reason["end_of_backtest"]["win_rate"] == 0.0


def test_strategy_assessment_flags_at_risk_performance() -> None:
    assessment = build_strategy_assessment(
        trade_rows=_sample_trades(),
        metrics={
            "sharpe": -1.5,
            "max_drawdown": -0.6,
            "total_return": -0.5,
            "trade_count": 3,
            "avg_holding_days": 3.7,
        },
        config={"codes": ["AAA", "BBB", "CCC"], "portfolio": {"rebalance": "weekly"}},
    )
    assert assessment["grade"] in {"poor", "fair"}
    by_id = {check["id"]: check for check in assessment["checks"]}
    assert by_id["performance"]["status"] == "fail"
    assert by_id["universe"]["status"] in {"pass", "warn"}
    assert len(assessment["highlights"]["top_winners"]) == 2
    assert len(assessment["highlights"]["top_losers"]) == 1


def test_build_run_report_extras_bundle() -> None:
    extras = build_run_report_extras(
        trade_rows=_sample_trades(),
        metrics={"sharpe": 1.2, "max_drawdown": -0.1, "total_return": 0.2, "trade_count": 3},
    )
    assert len(extras["by_symbol"]) == 2
    assert extras["by_exit_reason"]
    assert "score" in extras["strategy_assessment"]


def test_breakeven_exit_counted_via_holding_days() -> None:
    """pnl==0 exits must still appear when holding_days > 0."""
    rows = build_symbol_breakdown(
        [
            {"code": "ZZZ", "side": "buy", "pnl": 0, "holding_days": 0, "return_pct": 0, "reason": "signal"},
            {"code": "ZZZ", "side": "sell", "pnl": 0, "holding_days": 2, "return_pct": 0, "reason": "signal"},
        ]
    )
    assert len(rows) == 1
    assert rows[0]["count"] == 1
    assert rows[0]["total_pnl"] == 0.0
