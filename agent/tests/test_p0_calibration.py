"""P0 calibration tests: T+1 timing, ProcessMandate wiring, open limit checks, STAR lots, ex-dividend."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from backtest.models import Position
from src.monitor.models import HoldingRow, MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.scan import run_monitor_scan
from src.paper_book.book import _bar_on, step_day
from src.paper_book.signals import resolve_intents
from src.paper_book.store import load_state
from src.process.models import Decision, SymbolDecision


def test_open_normal_afternoon_limit_up_allows_open_buy_without_lookahead() -> None:
    """A stock opening at +2% that closes at +10% limit-up WAS fillable at 09:30 open."""
    engine = ChinaAEngine({})
    bar = pd.Series(
        {
            "open": 10.20,
            "high": 11.00,
            "low": 10.10,
            "close": 11.00,
            "pre_close": 10.00,
            "volume": 50_000.0,
            "trade_date": pd.Timestamp("2026-01-05"),
        }
    )
    assert engine.can_execute("000001.SZ", 1, bar) is True

    # Conversely, opening sealed at +10% limit-up is unfillable at 09:30 even if it broke board later.
    sealed_open = pd.Series(
        {
            "open": 11.00,
            "high": 11.00,
            "low": 10.10,
            "close": 10.20,
            "pre_close": 10.00,
            "volume": 50_000.0,
            "trade_date": pd.Timestamp("2026-01-05"),
        }
    )
    assert engine.can_execute("000001.SZ", 1, sealed_open) is False


def test_open_normal_afternoon_limit_down_allows_open_sell_without_lookahead() -> None:
    """A stock opening at -2% that later closes at -10% limit-down WAS sellable at 09:30 open."""
    engine = ChinaAEngine({})
    engine.positions["000001.SZ"] = Position(
        "000001.SZ", 1, 10.0, pd.Timestamp("2026-01-02"), 500.0
    )
    bar = pd.Series(
        {
            "open": 9.80,
            "high": 9.85,
            "low": 9.00,
            "close": 9.00,
            "pre_close": 10.00,
            "volume": 50_000.0,
            "trade_date": pd.Timestamp("2026-01-05"),
        }
    )
    assert engine.can_execute("000001.SZ", 0, bar) is True


def test_star_board_200_share_minimum_and_1_share_increment() -> None:
    engine = ChinaAEngine({})
    assert engine.round_size(199.0, 80.0, symbol="688001.SH") == 0
    assert engine.round_size(200.0, 80.0, symbol="688001.SH") == 200
    assert engine.round_size(237.9, 80.0, symbol="688001.SH") == 237
    # Mainboard remains 100-share lots.
    assert engine.round_size(237.9, 80.0, symbol="600519.SH") == 200


def test_paper_book_rejects_star_order_under_200_shares(tmp_path: Path) -> None:
    cfg = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_star"),
        suggestions=[
            MonitorSuggestion(
                symbol="688041.SH",
                action="buy",
                reason="signal",
                as_of="2026-01-05",
                tradable=True,
            )
        ],
        holdings=[HoldingRow(symbol="688041.SH", weight=0.15)],
        tradable=True,
    )
    # 100,000 NAV * 15% = 15,000 RMB; at 120 RMB/share desired is ~124 shares (< 200 min lot).
    bar = pd.Series(
        {
            "open": 120.0,
            "close": 120.0,
            "pre_close": 118.0,
            "volume": 100_000.0,
            "trade_date": pd.Timestamp("2026-01-05"),
        }
    )
    report = step_day(
        as_of="2026-01-05",
        config=cfg,
        root=tmp_path / "book",
        bars={"688041.SH": bar},
        initial_cash=100_000.0,
        max_position=0.20,
        strategy_store=None,
    )
    assert report.fills == []
    # Now with 25% cap & weight -> 25,000 / 120 = 208 shares (>= 200, 1-share increment).
    cfg.holdings = [HoldingRow(symbol="688041.SH", weight=0.25)]
    report2 = step_day(
        as_of="2026-01-06",
        config=MonitorConfig(
            strategy=StrategyRef(type="run_dir", run_id="run_star"),
            suggestions=[
                MonitorSuggestion(
                    symbol="688041.SH",
                    action="buy",
                    reason="signal",
                    as_of="2026-01-06",
                    tradable=True,
                )
            ],
            holdings=[HoldingRow(symbol="688041.SH", weight=0.25)],
            tradable=True,
        ),
        root=tmp_path / "book",
        bars={
            "688041.SH": pd.Series(
                {
                    "open": 120.0,
                    "close": 121.0,
                    "pre_close": 120.0,
                    "volume": 100_000.0,
                    "trade_date": pd.Timestamp("2026-01-06"),
                }
            )
        },
        initial_cash=100_000.0,
        max_position=0.30,
        strategy_store=None,
    )
    assert len(report2.fills) == 1
    assert report2.fills[0].shares >= 200
    assert report2.fills[0].shares != 200  # proves 1-share increment (e.g. 208)


def test_process_decision_tightens_and_blocks_paper_book_intents() -> None:
    cfg = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_proc"),
        suggestions=[
            MonitorSuggestion(symbol="000001.SZ", action="buy", reason="s", as_of="2026-01-06", tradable=True),
            MonitorSuggestion(symbol="000002.SZ", action="buy", reason="s", as_of="2026-01-06", tradable=True),
        ],
        holdings=[
            HoldingRow(symbol="000001.SZ", weight=0.50),
            HoldingRow(symbol="000002.SZ", weight=0.50),
        ],
        decision=Decision(
            action="enter",
            size_nav=0.20,
            reason="half_watch tightened",
            per_symbol=[
                SymbolDecision(
                    symbol="000001.SZ",
                    action="enter",
                    weight=0.20,
                    raw_weight=0.50,
                    raw_action="enter",
                ),
                SymbolDecision(
                    symbol="000002.SZ",
                    action="wait",
                    weight=0.0,
                    raw_weight=0.50,
                    raw_action="enter",
                    blocked=True,
                    block_reason="analog_down",
                ),
            ],
        ),
        tradable=True,
    )
    intents, skipped = resolve_intents(cfg, "2026-01-06")
    assert len(intents) == 1
    assert intents[0].symbol == "000001.SZ"
    assert intents[0].target_weight == pytest.approx(0.20)
    assert any(s["symbol"] == "000002.SZ" and s["reason"] == "analog_down" for s in skipped)


def test_settled_scan_stamps_next_session_and_includes_rotated_holdings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260101_000000_00_rot01"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        out = {}",
                "        for code, df in data_map.items():",
                "            s = pd.Series(0.0, index=df.index)",
                "            if code == '000002.SZ' and len(s):",
                "                s.iloc[-1] = 1.0",
                "            out[code] = s",
                "        return out",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"source": "auto", "interval": "1D", "codes": ["000001.SZ", "000002.SZ"]}),
        encoding="utf-8",
    )
    idx = pd.bdate_range("2026-01-01", "2026-01-05")
    frame = pd.DataFrame(
        {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 50_000.0},
        index=idx,
    )
    monkeypatch.setattr(
        "src.monitor.scan._fetch_universe_ohlcv",
        lambda universe, **kw: ({s: frame.copy() for s in universe}, {}),
    )
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda dm, **kw: (dm, {k: False for k in dm}, {}),
    )
    # 1. Subset watchlist: suggestions stays scoped to 000001.SZ, while tradable
    #    board resolve_intents picks up rotated holding 000002.SZ on T+1!
    cfg = MonitorConfig(
        symbols=["000001.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    res = run_monitor_scan(cfg, as_of=date(2026, 1, 5), runs_root=tmp_path / "runs")
    assert [s.symbol for s in res.suggestions] == ["000001.SZ"]
    assert res.suggestions[0].exec_as_of == "2026-01-06"
    assert {h.symbol for h in res.holdings} == {"000002.SZ"}

    res.tradable = True
    res.decision = None
    same_day_intents, _ = resolve_intents(res, "2026-01-05")
    assert same_day_intents == []
    next_day_intents, _ = resolve_intents(res, "2026-01-06")
    assert len(next_day_intents) == 1
    assert next_day_intents[0].symbol == "000002.SZ"

    # 2. Empty watchlist: suggestions auto-populates active holdings.
    empty_cfg = MonitorConfig(
        symbols=[],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    res_empty = run_monitor_scan(empty_cfg, as_of=date(2026, 1, 5), runs_root=tmp_path / "runs")
    assert [s.symbol for s in res_empty.suggestions] == ["000002.SZ"]
    assert res_empty.suggestions[0].action == "buy"
    assert res_empty.suggestions[0].exec_as_of == "2026-01-06"


def test_paper_book_conserves_nav_across_10_for_10_split_and_dividend(tmp_path: Path) -> None:
    """A 10转10派5元 ex-date halves the raw price while doubling shares + crediting cash."""
    frame = pd.DataFrame(
        {
            "open": [20.5, 10.0],
            "high": [20.6, 10.2],
            "low": [20.2, 9.9],
            "close": [20.5, 10.0],
            "volume": [100_000.0, 200_000.0],
            "adj_factor": [1.0, 2.05],
        },
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )
    day1_bar = _bar_on(frame, "2026-01-05")
    day2_bar = _bar_on(frame, "2026-01-06")
    assert day1_bar is not None and day2_bar is not None

    cfg1 = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_split"),
        suggestions=[
            MonitorSuggestion(symbol="000001.SZ", action="buy", reason="s", as_of="2026-01-05", tradable=True)
        ],
        holdings=[HoldingRow(symbol="000001.SZ", weight=0.50)],
        tradable=True,
    )
    r1 = step_day(
        as_of="2026-01-05",
        config=cfg1,
        root=tmp_path / "book",
        bars={"000001.SZ": day1_bar},
        initial_cash=100_000.0,
        max_position=0.60,
        strategy_store=None,
    )
    shares_day1 = r1.fills[0].shares
    assert shares_day1 > 0

    cfg2 = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_split"),
        suggestions=[],
        tradable=True,
    )
    r2 = step_day(
        as_of="2026-01-06",
        config=cfg2,
        root=tmp_path / "book",
        bars={"000001.SZ": day2_bar},
        initial_cash=100_000.0,
        max_position=0.60,
        max_drawdown=0.15,
        strategy_store=None,
    )
    state = load_state(tmp_path / "book", "run_split")
    assert state is not None
    # Shares doubled from 10转10, cash gained ¥0.50 * shares_day1, and NAV stayed flat (0% return)!
    assert state.positions["000001.SZ"].shares == shares_day1 * 2
    assert r2.nav == pytest.approx(r1.nav, rel=1e-6)
    assert r2.downgraded is False
