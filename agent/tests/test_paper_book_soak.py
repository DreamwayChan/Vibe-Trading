"""Twenty consecutive A-share sessions: calendar, T+1, and the window report."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from backtest.engines.china_a import ChinaAEngine
from src.config.accessor import reset_env_config
from src.data import calendar as cal
from src.monitor.models import MonitorConfig, MonitorDaySnapshot, MonitorSuggestion, StrategyRef
from src.monitor.store import save_day_snapshot, save_monitor
from src.paper_book.book import _sell_intent, step_day
from src.paper_book.cli import main
from src.paper_book.models import PAPER_BOOK_DECAY, PAPER_BOOK_DEVIATION, Intent
from src.paper_book.soak import sessions_ending, step_range
from src.paper_book.store import PaperBookError, load_report, load_state, load_window
from src.strategy_store.models import Artifact, ArtifactStatus, ArtifactType
from src.strategy_store.store import InMemoryStrategyStore

AAA = "000001.SZ"
BBB = "000002.SZ"
CCC = "600000.SH"
EEE = "600001.SH"
RUN = "run1"


@pytest.fixture
def offline_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Weekday calendar in a temp lake. No network and no seeded holidays."""
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
    monkeypatch.setattr("src.data.lake.lake_root", lambda: tmp_path / "lake")
    cal.clear_cache()
    yield
    cal.clear_cache()


def _weekdays(start: str, count: int) -> list[str]:
    out: list[str] = []
    cur = dt.date.fromisoformat(start)
    while len(out) < count:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


def _bar(day: str, close: float = 10.0, volume: float = 1_000.0) -> pd.Series:
    return pd.Series(
        {
            "open": close,
            "close": close,
            "pre_close": 10.0,
            "volume": volume,
            "trade_date": pd.Timestamp(day),
        }
    )


def _suggestion(symbol: str, action: str, day: str) -> MonitorSuggestion:
    return MonitorSuggestion(
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        reason="signal",
        as_of=day,
        tradable=True,
    )


def _config(day_suggestions: list[MonitorSuggestion] | None = None) -> MonitorConfig:
    return MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=RUN),
        suggestions=list(day_suggestions or []),
        tradable=True,
    )


def _equity(run_dir: Path, days: list[str]) -> None:
    folder = run_dir / "artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"timestamp": "2026-01-02", "equity": 1_000_000.0}]
    rows.extend({"timestamp": day, "equity": 1_000_000.0} for day in days)
    pd.DataFrame(rows).to_csv(folder / "equity.csv", index=False)


def _bars_for(sessions: list[str]) -> dict[str, dict[str, pd.Series]]:
    out: dict[str, dict[str, pd.Series]] = {}
    for index, day in enumerate(sessions):
        close = 10.0 if index < 2 else 10.5
        names = {BBB: _bar(day, close=close)}
        if index <= 1:
            names[AAA] = _bar(day, close=10.0)
        if index == 12:
            names[EEE] = _bar(day, close=close, volume=0.0)
        if index == len(sessions) - 1:
            names[CCC] = _bar(day, close=close)
        out[day] = names
    return out


def _schedule(sessions: list[str]) -> dict[str, MonitorConfig]:
    last = sessions[-1]
    return {
        sessions[0]: _config([_suggestion(AAA, "buy", sessions[0])]),
        sessions[1]: _config(
            [
                _suggestion(AAA, "sell", sessions[1]),
                _suggestion(BBB, "buy", sessions[1]),
            ]
        ),
        sessions[12]: _config([_suggestion(EEE, "buy", sessions[12])]),
        last: _config([_suggestion(CCC, "buy", last)]),
    }


def test_soak_twenty_sessions_carries_positions_and_tplus1(
    tmp_path: Path,
    offline_calendar: None,
) -> None:
    sessions = _weekdays("2026-01-05", 20)
    assert sessions[0] == "2026-01-05"
    assert sessions[-1] == "2026-01-30"
    assert "2026-01-10" not in sessions  # Saturday
    assert "2026-01-11" not in sessions  # Sunday

    run_dir = tmp_path / "runs" / RUN
    _equity(run_dir, sessions)
    root = tmp_path / "book"
    engine = ChinaAEngine({})
    window = step_range(
        start=sessions[0],
        end=sessions[-1],
        config=_config(),
        run_dir=run_dir,
        root=root,
        bars_by_day=_bars_for(sessions),
        configs_by_day=_schedule(sessions),
        engine=engine,
        threshold=1.0,
        initial_cash=1_000_000.0,
        max_position=0.40,
        max_drawdown=0.50,
        strategy_store=None,
        use_snapshots=False,
    )

    assert window.calendar == "weekday"
    assert window.session_count == 20
    assert window.sessions == sessions
    assert all(pd.Timestamp(day).weekday() < 5 for day in window.sessions)
    assert len(window.days) == 20

    first, second = window.days[0], window.days[1]
    assert first["fills"][0]["side"] == "buy"
    assert first["fills"][0]["symbol"] == AAA
    assert second["fills"][0]["side"] == "sell"
    assert second["fills"][0]["symbol"] == AAA
    assert second["fills"][0]["shares"] == first["fills"][0]["shares"]
    bbb_fill = next(fill for fill in second["fills"] if fill["symbol"] == BBB)
    assert bbb_fill["side"] == "buy"
    assert bbb_fill["shares"] > 0 and bbb_fill["shares"] % 100 == 0

    held_bbb = bbb_fill["shares"]
    for row in window.days[1:]:
        bbb = next(item for item in load_report(root, RUN, row["as_of"])["positions"] if item["symbol"] == BBB)
        assert bbb["shares"] == held_bbb

    for prev, nxt in zip(window.days, window.days[1:]):
        assert nxt["prev_nav"] == pytest.approx(prev["nav"])
        assert nxt["cash"] >= 0

    suspended = window.days[12]
    assert suspended["unfilled"][0]["symbol"] == EEE
    assert suspended["unfilled"][0]["reason"] == "suspended"
    assert suspended["fill_count"] == 0

    last = window.days[-1]
    assert last["fills"][0]["symbol"] == CCC
    assert last["fills"][0]["side"] == "buy"
    ccc_shares = last["fills"][0]["shares"]
    assert ccc_shares > 0

    state = load_state(root, RUN)
    assert state is not None
    assert state.settled_through == sessions[-1]
    assert state.inception_date == sessions[0]
    assert AAA not in state.positions
    assert state.positions[BBB].shares == held_bbb
    assert state.positions[BBB].lots[0].buy_date == sessions[1]
    assert state.positions[CCC].shares == ccc_shares
    assert state.positions[CCC].lots[0].buy_date == sessions[-1]

    # Bought on the last session: T+1 still locks it against that same date.
    locked, rows = _sell_intent(
        state,
        day=sessions[-1],
        intent=Intent(CCC, "sell", 0.0),
        bars={CCC: _bar(sessions[-1], close=10.5)},
        engine=engine,
    )
    assert locked is None
    assert rows[0].reason == "t_plus_one"
    assert state.positions[CCC].shares == ccc_shares

    again = step_range(
        start=sessions[0],
        end=sessions[-1],
        config=_config(),
        run_dir=run_dir,
        root=root,
        bars_by_day=_bars_for(sessions),
        configs_by_day=_schedule(sessions),
        engine=engine,
        threshold=1.0,
        initial_cash=1_000_000.0,
        max_position=0.40,
        max_drawdown=0.50,
        strategy_store=None,
        use_snapshots=False,
    )
    assert again.nav == pytest.approx(window.nav)
    assert again.days[-1]["signal_source"] == "replay"
    replayed = load_state(root, RUN)
    assert replayed is not None
    assert replayed.positions[BBB].shares == held_bbb
    assert replayed.positions[CCC].shares == ccc_shares

    follow = "2026-02-02"
    sold = step_day(
        as_of=follow,
        config=_config([_suggestion(CCC, "sell", follow)]),
        book_id=RUN,
        run_dir=run_dir,
        root=root,
        bars={
            CCC: _bar(follow, close=10.5),
            BBB: _bar(follow, close=10.5),
        },
        engine=engine,
        threshold=1.0,
        max_position=0.40,
        max_drawdown=0.50,
        strategy_store=None,
    )
    assert sold.fills[0].symbol == CCC
    assert sold.fills[0].side == "sell"
    assert sold.fills[0].shares == ccc_shares
    assert BBB in {row["symbol"] for row in sold.positions}

    deviations = [float(row["return_deviation"]) for row in window.days]
    assert window.return_deviation == pytest.approx(deviations[-1])
    assert window.return_deviation > 0
    assert window.max_return_deviation == pytest.approx(max(deviations))
    assert window.expectation["available"] is True
    assert window.expectation["threshold"] == pytest.approx(1.0)
    assert window.turnover == pytest.approx(sum(float(row["turnover"]) for row in window.days))
    assert window.fill_count == sum(row["fill_count"] for row in window.days)
    assert window.unfilled_count == sum(row["unfilled_count"] for row in window.days)
    # Twenty sessions of this path lose their recent Sharpe, so decay (default
    # on) clears tradable after the window. Fills inside the window already
    # settled; the flag blocks what comes next.
    assert window.decay["decayed"] is True
    assert window.decay["cleared_tradable"] is True
    assert window.downgraded is True
    assert window.downgrade_reason == PAPER_BOOK_DECAY

    reports = root / RUN / "reports"
    assert (reports / "window.json").is_file()
    named = reports / f"window_{sessions[0]}_{sessions[-1]}.json"
    assert named.is_file()
    assert load_window(root, RUN)["session_count"] == 20
    for day in sessions:
        assert (reports / f"{day}.json").is_file()


def test_deviation_downgrade_blocks_the_rest_of_the_window(
    tmp_path: Path,
    offline_calendar: None,
) -> None:
    sessions = _weekdays("2026-01-05", 3)
    run_dir = tmp_path / "runs" / RUN
    _equity(run_dir, sessions)
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(
        Artifact(
            id="",
            type=ArtifactType.STRATEGY,
            name="paper-demo",
            universe="ashare",
            run_dir=str(run_dir),
            status=ArtifactStatus.MONITORING,
            tradable=True,
            validation_gate=json.dumps({"passed": True, "reasons": []}),
        )
    )
    board = _config()
    configs = {day: _config([_suggestion(AAA, "buy", day)]) for day in sessions}
    bars = {day: {AAA: _bar(day)} for day in sessions}
    window = step_range(
        start=sessions[0],
        end=sessions[-1],
        config=board,
        run_dir=run_dir,
        root=tmp_path / "book",
        bars_by_day=bars,
        configs_by_day=configs,
        engine=ChinaAEngine({}),
        threshold=0.0,
        max_position=0.40,
        strategy_store=store,
        use_snapshots=False,
    )

    assert window.downgraded is True
    assert window.downgrade_reason == PAPER_BOOK_DEVIATION
    assert window.downgraded_on == sessions[0]
    assert window.days[0]["fill_count"] == 1
    assert window.days[1]["fill_count"] == 0
    assert window.days[2]["fill_count"] == 0
    assert board.tradable is False
    assert PAPER_BOOK_DEVIATION in board.tradable_reasons
    artifact = store.get_artifact(artifact_id)
    assert artifact is not None
    assert artifact.tradable is False
    assert artifact.status is ArtifactStatus.MONITORING
    assert store.get_decay_history(artifact_id) == []
    skipped = load_report(tmp_path / "book", RUN, sessions[1])["skipped"]
    assert PAPER_BOOK_DEVIATION in skipped[0]["reason"]


def test_exchange_calendar_skips_spring_festival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.data.lake.lake_root", lambda: tmp_path / "lake")
    monkeypatch.delenv("VIBE_TRADING_CALENDAR_OFFLINE", raising=False)
    cal.clear_cache()
    closed = {dt.date(2024, 2, day) for day in range(9, 18)}

    def _fake() -> list[dt.date]:
        out: list[dt.date] = []
        cur = dt.date(2024, 1, 1)
        while cur <= dt.date(2024, 3, 31):
            if cur.weekday() < 5 and cur not in closed:
                out.append(cur)
            cur += dt.timedelta(days=1)
        return out

    monkeypatch.setattr(cal, "_fetch_from_akshare", _fake)
    try:
        cal.refresh_calendar()
        monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
        window = step_range(
            start="2024-02-05",
            end="2024-02-23",
            config=_config(),
            root=tmp_path / "book",
            engine=ChinaAEngine({}),
            threshold=1.0,
            strategy_store=None,
            use_snapshots=False,
        )
    finally:
        cal.clear_cache()

    assert window.calendar == "exchange"
    assert "2024-02-08" in window.sessions  # Thursday before the holiday
    assert "2024-02-09" not in window.sessions  # Friday, Spring Festival
    assert "2024-02-16" not in window.sessions
    assert "2024-02-17" not in window.sessions  # Sunday inside the closure
    assert "2024-02-19" in window.sessions  # first session after the holiday
    assert all(pd.Timestamp(day).weekday() < 5 for day in window.sessions)
    assert window.fill_count == 0
    assert window.session_count == len(window.days)
    assert window.nav == pytest.approx(window.initial_cash)


def test_weekend_range_has_no_sessions(tmp_path: Path, offline_calendar: None) -> None:
    with pytest.raises(PaperBookError, match="no trading sessions"):
        step_range(
            start="2026-01-10",
            end="2026-01-11",
            config=_config(),
            root=tmp_path / "book",
            strategy_store=None,
            use_snapshots=False,
        )


def test_sessions_ending_walks_back_from_a_sunday(offline_calendar: None) -> None:
    assert sessions_ending("2026-01-11", 1) == ["2026-01-09"]
    twenty = sessions_ending("2026-01-30", 20)
    assert len(twenty) == 20
    assert twenty[0] == "2026-01-05"
    assert twenty[-1] == "2026-01-30"


def test_soak_uses_the_monitor_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_calendar: None
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    day = "2026-01-06"
    save_day_snapshot(
        MonitorDaySnapshot(
            as_of=day,
            scanned_at="2026-01-06T15:00:00Z",
            run_id=RUN,
            suggestions=[_suggestion(AAA, "buy", day)],
            tradable=True,
        )
    )
    window = step_range(
        start="2026-01-05",
        end=day,
        config=_config(),
        root=tmp_path / "book",
        bars_by_day={
            "2026-01-05": {AAA: _bar("2026-01-05")},
            day: {AAA: _bar(day)},
        },
        engine=ChinaAEngine({}),
        threshold=1.0,
        max_position=0.40,
        strategy_store=None,
    )
    assert [row["signal_source"] for row in window.days] == ["flat", "snapshot"]
    assert window.days[0]["fill_count"] == 0
    assert window.days[1]["fills"][0]["symbol"] == AAA


def test_cli_soak_twenty_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline_calendar: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "src.paper_book.book.load_day_bars",
        lambda symbols, as_of: {symbol: _bar(as_of) for symbol in symbols},
    )
    schedule = {
        "2026-01-05": [{"symbol": AAA, "action": "buy"}],
        "2026-01-06": [{"symbol": AAA, "action": "sell"}],
    }
    path = tmp_path / "signals.json"
    path.write_text(json.dumps(schedule), encoding="utf-8")
    root = tmp_path / "book"
    code = main(
        [
            "soak",
            "--days",
            "20",
            "--end",
            "2026-01-30",
            "--run-id",
            RUN,
            "--root",
            str(root),
            "--signals",
            str(path),
        ]
    )
    assert code == 0, capsys.readouterr()
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_count"] == 20
    assert payload["sessions"][0] == "2026-01-05"
    assert payload["sessions"][-1] == "2026-01-30"
    assert payload["calendar"] == "weekday"
    assert "2026-01-10" not in payload["sessions"]
    bought = payload["days"][0]["fills"][0]
    sold = payload["days"][1]["fills"][0]
    assert bought["side"] == "buy"
    assert sold["side"] == "sell"
    assert sold["shares"] == bought["shares"]
    assert payload["fill_count"] == 2
    assert (root / RUN / "reports" / "window.json").is_file()

    assert main(["show", "--book-id", RUN, "--root", str(root), "--window"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["window"]["session_count"] == 20
    assert shown["report"]["as_of"] == "2026-01-30"

    assert (
        main(
            [
                "soak",
                "--from",
                "2026-01-05",
                "--to",
                "2026-01-30",
                "--days",
                "20",
                "--root",
                str(root),
            ]
        )
        == 1
    )
    assert main(["step", "--from", "2026-01-10", "--to", "2026-01-11", "--root", str(root)]) == 1


def test_api_soak_twenty_weekdays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_calendar: None) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("VIBE_TRADING_PAPER_BOOK_ROOT", str(tmp_path / "book"))
    reset_env_config()
    save_monitor(_config([_suggestion(AAA, "buy", "2026-01-05")]))
    monkeypatch.setattr(
        "src.paper_book.book.load_day_bars",
        lambda symbols, as_of: {symbol: _bar(as_of) for symbol in symbols},
    )
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    response = client.post(
        "/paper-book/soak",
        json={"end": "2026-01-30", "days": 20, "run_id": RUN},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_count"] == 20
    assert body["sessions"][0] == "2026-01-05"
    assert body["sessions"][-1] == "2026-01-30"
    assert body["days"][0]["signal_source"] == "board"
    assert body["days"][0]["fill_count"] == 1
    assert body["days"][1]["signal_source"] == "flat"
    assert body["days"][1]["fill_count"] == 0

    fetched = client.get("/paper-book/run1", params={"window": True})
    assert fetched.status_code == 200
    assert fetched.json()["window"]["nav"] == pytest.approx(body["nav"])

    weekend = client.post(
        "/paper-book/soak",
        json={"start": "2026-01-10", "end": "2026-01-11", "run_id": RUN},
    )
    assert weekend.status_code == 400
