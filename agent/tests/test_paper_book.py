"""Paper book day-step: A-share fills, T+1, limit-up, and deviation downgrade."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from backtest.engines.china_a import ChinaAEngine
from src.config.accessor import reset_env_config
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.store import load_board_for_strategy, save_monitor
from src.paper_book.book import _sell_intent, step_day
from src.paper_book.cli import main
from src.paper_book.models import PAPER_BOOK_DEVIATION, Intent
from src.paper_book.store import PaperBookError, load_state
from src.strategy_store.models import Artifact, ArtifactStatus, ArtifactType
from src.strategy_store.store import InMemoryStrategyStore

DAY = "2026-01-05"
NEXT = "2026-01-06"
SYMBOL = "000001.SZ"


def _bar(**extra) -> pd.Series:
    data = {
        "open": 10.0,
        "close": 10.0,
        "pre_close": 10.0,
        "volume": 1_000.0,
        "trade_date": pd.Timestamp(DAY),
    }
    data.update(extra)
    return pd.Series(data)


def _suggestion(
    *,
    action: str = "buy",
    symbol: str = SYMBOL,
    as_of: str = DAY,
    tradable: bool = True,
    provisional: bool = False,
    block_reasons: list[str] | None = None,
) -> MonitorSuggestion:
    return MonitorSuggestion(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        reason="signal",
        as_of=as_of,
        tradable=tradable,
        provisional=provisional,
        block_reasons=list(block_reasons or []),
    )


def _config(suggestions: list[MonitorSuggestion], *, run_id: str = "run1") -> MonitorConfig:
    return MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=run_id),
        suggestions=suggestions,
        tradable=any(item.tradable for item in suggestions),
    )


def _equity(run_dir: Path, rows: list[dict]) -> None:
    folder = run_dir / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(folder / "equity.csv", index=False)


def _flat_run(run_dir: Path) -> None:
    _equity(
        run_dir,
        [
            {"timestamp": "2026-01-02", "equity": 1_000_000.0},
            {"timestamp": DAY, "equity": 1_000_000.0},
            {"timestamp": NEXT, "equity": 1_000_000.0},
        ],
    )


def _artifact(run_dir: Path) -> Artifact:
    return Artifact(
        id="",
        type=ArtifactType.STRATEGY,
        name="paper-demo",
        universe="ashare",
        run_dir=str(run_dir),
        status=ArtifactStatus.MONITORING,
        tradable=True,
        validation_gate=json.dumps({"passed": True, "reasons": []}),
    )


def _step(tmp_path: Path, config: MonitorConfig, bars: dict, **kwargs):
    engine = kwargs.pop("engine", None) or ChinaAEngine({})
    return step_day(
        as_of=kwargs.pop("as_of", DAY),
        config=config,
        root=kwargs.pop("root", tmp_path / "book"),
        bars=bars,
        engine=engine,
        threshold=kwargs.pop("threshold", 1.0),
        initial_cash=kwargs.pop("initial_cash", 1_000_000.0),
        strategy_store=kwargs.pop("strategy_store", None),
        **kwargs,
    )


def test_buy_fill_matches_china_a_costs(tmp_path: Path) -> None:
    engine = ChinaAEngine({})
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar()},
        engine=engine,
        max_position=1.0,
    )

    assert len(report.fills) == 1
    fill = report.fills[0]
    assert fill.side == "buy"
    assert fill.shares % 100 == 0
    assert fill.shares > 0
    assert fill.fill_price == pytest.approx(engine.apply_slippage(10.0, 1))
    assert fill.commission == pytest.approx(
        engine.calc_commission(fill.shares, fill.fill_price, 1, is_open=True)
    )
    assert report.cash == pytest.approx(1_000_000.0 - fill.notional - fill.commission)
    assert report.nav == pytest.approx(report.cash + fill.shares * 10.0)
    assert report.turnover == pytest.approx(fill.notional / (2.0 * 1_000_000.0))
    # One more board lot would not fit, so the size is the cash-constrained lot.
    bigger = fill.shares + 100
    extra = engine.calc_commission(bigger, fill.fill_price, 1, is_open=True)
    assert bigger * fill.fill_price + extra > 1_000_000.0

    state = load_state(tmp_path / "book", "run1")
    assert state is not None
    assert state.positions[SYMBOL].shares == fill.shares
    assert state.positions[SYMBOL].lots[0].buy_date == DAY


def test_same_day_sell_is_blocked_and_next_session_sells(tmp_path: Path) -> None:
    engine = ChinaAEngine({})
    bars = {SYMBOL: _bar()}
    bought = _step(tmp_path, _config([_suggestion()]), bars, engine=engine)
    state = load_state(tmp_path / "book", "run1")
    assert state is not None
    fill, rows = _sell_intent(
        state,
        day=DAY,
        intent=Intent(SYMBOL, "sell", 0.0),
        bars=bars,
        engine=engine,
    )
    assert fill is None
    assert rows[0].reason == "t_plus_one"
    assert state.positions[SYMBOL].shares == bought.fills[0].shares

    next_bars = {SYMBOL: _bar(trade_date=pd.Timestamp(NEXT))}
    sold = _step(
        tmp_path,
        _config([_suggestion(action="sell", as_of=NEXT)]),
        next_bars,
        as_of=NEXT,
        engine=engine,
    )
    assert len(sold.fills) == 1
    sale = sold.fills[0]
    assert sale.side == "sell"
    assert sale.shares == bought.fills[0].shares
    assert sale.fill_price == pytest.approx(engine.apply_slippage(10.0, -1))
    assert sale.commission == pytest.approx(
        engine.calc_commission(sale.shares, sale.fill_price, 0, is_open=False)
    )
    open_fee = engine.calc_commission(sale.shares, sale.fill_price, 0, is_open=True)
    assert sale.commission - open_fee == pytest.approx(sale.notional * engine.stamp_tax)
    assert sold.positions == []
    assert sold.unfilled == []


def test_tplus1_off_allows_same_day_sell(tmp_path: Path) -> None:
    engine = ChinaAEngine({"t_plus_one": False})
    bars = {SYMBOL: _bar()}
    bought = _step(tmp_path, _config([_suggestion()]), bars, engine=engine)
    state = load_state(tmp_path / "book", "run1")
    assert state is not None
    fill, rows = _sell_intent(
        state,
        day=DAY,
        intent=Intent(SYMBOL, "sell", 0.0),
        bars=bars,
        engine=engine,
    )
    assert rows == []
    assert fill is not None
    assert fill.shares == bought.fills[0].shares
    assert SYMBOL not in state.positions


def test_limit_up_buy_is_unfilled(tmp_path: Path) -> None:
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar(open=11.0, close=11.0, pre_close=10.0)},
    )
    assert report.fills == []
    assert report.unfilled[0].reason == "limit_up"
    assert report.unfilled[0].shares > 0
    assert report.nav == pytest.approx(report.initial_cash)
    assert report.cash == pytest.approx(report.initial_cash)


def test_st_buy_is_unfilled(tmp_path: Path) -> None:
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar(is_st=True)},
    )
    assert report.fills == []
    assert report.unfilled[0].reason == "st"
    assert report.unfilled[0].shares > 0
    assert report.nav == pytest.approx(report.initial_cash)


def test_growth_board_ten_percent_move_still_fills(tmp_path: Path) -> None:
    symbol = "300750.SZ"
    report = _step(
        tmp_path,
        _config([_suggestion(symbol=symbol)]),
        {symbol: _bar(open=11.0, close=11.0, pre_close=10.0)},
    )
    assert report.fills
    assert report.fills[0].symbol == symbol
    assert report.unfilled == []


def test_suspended_name_is_unfilled(tmp_path: Path) -> None:
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar(volume=0.0)},
    )
    assert report.fills == []
    assert report.unfilled[0].reason == "suspended"
    assert report.cash == pytest.approx(report.initial_cash)


def test_limit_down_sell_is_unfilled(tmp_path: Path) -> None:
    engine = ChinaAEngine({})
    _step(tmp_path, _config([_suggestion()]), {SYMBOL: _bar()}, engine=engine)
    report = _step(
        tmp_path,
        _config([_suggestion(action="sell", as_of=NEXT)]),
        {SYMBOL: _bar(open=9.0, close=9.0, pre_close=10.0, trade_date=pd.Timestamp(NEXT))},
        as_of=NEXT,
        engine=engine,
    )
    assert report.fills == []
    assert report.unfilled[0].reason == "limit_down"
    assert report.positions[0]["shares"] > 0


def test_non_tradable_and_stale_signals_do_not_trade(tmp_path: Path) -> None:
    config = _config(
        [
            _suggestion(tradable=False, block_reasons=["missing_journal"]),
            _suggestion(symbol="600000.SH", as_of="2026-01-02"),
            _suggestion(action="hold"),
        ]
    )
    report = _step(
        tmp_path,
        config,
        {SYMBOL: _bar(), "600000.SH": _bar()},
    )
    assert report.fills == []
    assert report.nav == pytest.approx(report.initial_cash)
    reasons = {row["reason"] for row in report.skipped}
    assert "missing_journal" in reasons
    assert "stale_signal" in reasons


def test_restep_does_not_double_the_fill(tmp_path: Path) -> None:
    config = _config([_suggestion()])
    bars = {SYMBOL: _bar()}
    first = _step(tmp_path, config, bars)
    second = _step(tmp_path, config, bars)
    assert second.fills[0].shares == first.fills[0].shares
    assert second.nav == pytest.approx(first.nav)
    assert second.cash == pytest.approx(first.cash)
    state = load_state(tmp_path / "book", "run1")
    assert state is not None
    assert state.positions[SYMBOL].shares == first.fills[0].shares


def test_deviation_downgrades_tradable_without_touching_decay_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run1"
    _flat_run(run_dir)
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(_artifact(run_dir))
    config = _config([_suggestion()])
    report = _step(
        tmp_path,
        config,
        {SYMBOL: _bar()},
        run_dir=run_dir,
        strategy_store=store,
        threshold=0.0,
    )

    assert report.downgraded is True
    assert report.downgrade_reason == PAPER_BOOK_DEVIATION
    assert report.expectation["available"] is True
    assert report.expectation["exceeded"] is True
    assert report.expectation["return_deviation"] > 0

    replay = _step(
        tmp_path,
        config,
        {SYMBOL: _bar()},
        run_dir=run_dir,
        strategy_store=store,
        threshold=0.0,
    )
    assert replay.fills[0].shares == report.fills[0].shares
    assert replay.nav == pytest.approx(report.nav)

    artifact = store.get_artifact(artifact_id)
    assert artifact is not None
    assert artifact.status is ArtifactStatus.MONITORING
    assert artifact.disabled_at is None
    assert artifact.tradable is False
    gate = json.loads(artifact.validation_gate or "{}")
    assert gate["passed"] is True
    assert gate["paper_book"]["reason"] == PAPER_BOOK_DEVIATION
    assert store.get_decay_history(artifact_id) == []

    assert config.tradable is False
    assert PAPER_BOOK_DEVIATION in config.tradable_reasons
    assert config.suggestions[0].tradable is False

    # The downgrade sticks for the next session: no new open.
    follow = _step(
        tmp_path,
        config,
        {SYMBOL: _bar(trade_date=pd.Timestamp(NEXT))},
        as_of=NEXT,
        run_dir=run_dir,
        strategy_store=store,
        threshold=0.0,
    )
    assert follow.fills == []
    assert store.get_artifact(artifact_id).status is ArtifactStatus.MONITORING


def test_deviation_inside_threshold_keeps_tradable(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run1"
    _flat_run(run_dir)
    (run_dir / "artifacts" / "trades.csv").write_text(
        "timestamp,code,side,price,qty,reason,pnl,holding_days,return_pct\n"
        f"{DAY},{SYMBOL},buy,10.0,100,signal,0,0,0\n",
        encoding="utf-8",
    )
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(_artifact(run_dir))
    config = _config([_suggestion()])
    report = _step(
        tmp_path,
        config,
        {SYMBOL: _bar()},
        run_dir=run_dir,
        strategy_store=store,
        threshold=0.5,
    )
    assert report.downgraded is False
    assert report.expectation["exceeded"] is False
    assert report.expectation["slippage_gap"] == pytest.approx(
        (report.fills[0].fill_price - 10.0) / 10.0
    )
    artifact = store.get_artifact(artifact_id)
    assert artifact is not None
    assert artifact.tradable is True
    assert artifact.status is ArtifactStatus.MONITORING
    assert config.tradable is True


def test_threshold_comes_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "runs" / "run1"
    _flat_run(run_dir)
    monkeypatch.setenv("VIBE_TRADING_PAPER_BOOK_DEVIATION_THRESHOLD", "0")
    reset_env_config()
    report = step_day(
        as_of=DAY,
        config=_config([_suggestion()]),
        root=tmp_path / "book",
        bars={SYMBOL: _bar()},
        engine=ChinaAEngine({}),
        run_dir=run_dir,
        strategy_store=None,
        initial_cash=1_000_000.0,
    )
    assert report.expectation["threshold"] == pytest.approx(0.0)
    assert report.downgraded is True


def test_cli_step_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    save_monitor(_config([_suggestion()]))
    monkeypatch.setattr(
        "src.paper_book.book.load_day_bars",
        lambda symbols, as_of: {SYMBOL: _bar()},
    )
    root = tmp_path / "book"
    assert main(["step", "--date", DAY, "--run-id", "run1", "--root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fills"][0]["symbol"] == SYMBOL
    assert main(["show", "--book-id", "run1", "--root", str(root)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["report"]["as_of"] == DAY
    assert main(["step", "--date", "not-a-date", "--root", str(root)]) == 1


def test_api_steps_one_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("VIBE_TRADING_PAPER_BOOK_ROOT", str(tmp_path / "book"))
    reset_env_config()
    save_monitor(_config([_suggestion()]))
    monkeypatch.setattr(
        "src.paper_book.book.load_day_bars",
        lambda symbols, as_of: {SYMBOL: _bar()},
    )
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    response = client.post("/paper-book/step", json={"as_of": DAY, "run_id": "run1"})
    assert response.status_code == 200
    body = response.json()
    assert body["book_id"] == "run1"
    assert body["fills"]
    assert body["downgraded"] is False

    fetched = client.get("/paper-book/run1")
    assert fetched.status_code == 200
    assert fetched.json()["report"]["nav"] == pytest.approx(body["nav"])

    missing = client.get("/paper-book/missing-book")
    assert missing.status_code == 404
    bad = client.post("/paper-book/step", json={"as_of": "05-01-2026", "run_id": "run1"})
    assert bad.status_code == 400

    # The board file is still the one the CLI/API loaded.
    board = load_board_for_strategy(StrategyRef(type="run_dir", run_id="run1"))
    assert board.suggestions[0].tradable is True


def test_default_position_cap_clips_a_full_weight_buy(tmp_path: Path) -> None:
    report = _step(tmp_path, _config([_suggestion()]), {SYMBOL: _bar()})
    fill = report.fills[0]
    assert fill.shares * fill.fill_price <= 0.20 * report.initial_cash + fill.fill_price
    assert (fill.shares + 100) * fill.fill_price > 0.20 * report.initial_cash
    assert report.risk["clipped"] == [SYMBOL]
    assert report.intents[0]["target_weight"] == pytest.approx(0.20)
    again = _step(tmp_path, _config([_suggestion()]), {SYMBOL: _bar()})
    assert again.fills[0].shares == fill.shares
    assert again.risk["clipped"] == [SYMBOL]
    assert again.intents[0]["target_weight"] == pytest.approx(0.20)


def test_volume_ratio_cap_clips_buy_size(tmp_path: Path) -> None:
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar(volume=1_000.0)},
        max_position=1.0,
        max_volume_ratio=0.20,
    )
    assert len(report.fills) == 1
    assert report.fills[0].shares == 200
    assert report.unfilled == []
    assert report.risk["max_volume_ratio"] == pytest.approx(0.20)
    assert report.risk["liquidity_clipped"] == [SYMBOL]


def test_volume_ratio_cap_blocks_under_one_lot(tmp_path: Path) -> None:
    report = _step(
        tmp_path,
        _config([_suggestion()]),
        {SYMBOL: _bar(volume=500.0)},
        max_position=1.0,
        max_volume_ratio=0.10,
    )
    assert report.fills == []
    assert report.unfilled[0].reason == "liquidity_cap"
    assert report.risk["liquidity_clipped"] == [SYMBOL]


def test_drawdown_cap_blocks_new_buys(tmp_path: Path) -> None:
    _step(tmp_path, _config([_suggestion()]), {SYMBOL: _bar()}, max_drawdown=0.05)
    marked = _step(
        tmp_path,
        _config([]),
        {SYMBOL: _bar(close=6.0, open=6.0, trade_date=pd.Timestamp(NEXT))},
        as_of=NEXT,
        max_drawdown=0.05,
    )
    assert marked.fills == []
    assert marked.risk["drawdown"] > 0.05

    third = "2026-01-07"
    blocked = _step(
        tmp_path,
        _config([_suggestion(as_of=third)]),
        {SYMBOL: _bar(close=6.0, open=6.0, trade_date=pd.Timestamp(third))},
        as_of=third,
        max_drawdown=0.05,
    )
    assert blocked.fills == []
    assert blocked.skipped[0]["reason"] == "drawdown_cap"
    assert blocked.risk["drawdown_blocked"] is True


def test_cannot_restep_an_earlier_session(tmp_path: Path) -> None:
    _step(tmp_path, _config([_suggestion()]), {SYMBOL: _bar()})
    with pytest.raises(PaperBookError):
        _step(
            tmp_path,
            _config([_suggestion(as_of="2026-01-02")]),
            {SYMBOL: _bar(trade_date=pd.Timestamp("2026-01-02"))},
            as_of="2026-01-02",
        )
