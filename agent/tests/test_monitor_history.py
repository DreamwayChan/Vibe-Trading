"""Holdings derivation, universe load, and daily snapshot persistence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from src.monitor.holdings import build_holdings_and_ops, weights_from_signal_map
from src.monitor.models import DayOps, HoldingRow, MonitorConfig, StrategyRef
from src.monitor.scan import run_monitor_scan
from src.monitor.store import (
    list_snapshot_summaries,
    load_day_snapshot,
    load_previous_snapshot,
    save_day_snapshot,
)
from src.monitor.universe import load_run_universe
from src.monitor.models import MonitorDaySnapshot


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_load_run_universe_prefers_config_codes(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(
        json.dumps({"codes": ["AAA.SZ", "BBB.SH", "aaa.sz"]}),
        encoding="utf-8",
    )
    codes = load_run_universe(run_dir, fallback=["ZZZ.SZ"])
    assert codes == ["AAA.SZ", "BBB.SH"]


def test_load_run_universe_falls_back_to_list_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"interval": "1D"}), encoding="utf-8")
    (run_dir / "all_bk1184_stocks.txt").write_text("111.SZ\n222.SH\n", encoding="utf-8")
    codes = load_run_universe(run_dir)
    assert codes == ["111.SZ", "222.SH"]


def test_build_holdings_ops_enter_exit_hold() -> None:
    holdings, ops = build_holdings_and_ops(
        {"AAA.SZ": 0.1, "BBB.SZ": 0.2},
        {"BBB.SZ": 0.15, "CCC.SZ": 0.1},
        meta_by_symbol={"AAA.SZ": {"price": 10.0}},
    )
    by_symbol = {h.symbol: h for h in holdings}
    assert set(by_symbol) == {"AAA.SZ", "BBB.SZ"}
    assert by_symbol["AAA.SZ"].action == "enter"
    assert by_symbol["BBB.SZ"].action == "add"
    assert ops.entered == ["AAA.SZ"]
    assert ops.exited == ["CCC.SZ"]
    assert ops.held == ["BBB.SZ"]


def test_weights_from_signal_map_keeps_latest_nonzero() -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    weights = weights_from_signal_map(
        {
            "AAA.SZ": pd.Series([0.0, 0.0, 0.25], index=idx),
            "BBB.SZ": pd.Series([0.1, 0.1, 0.0], index=idx),
        }
    )
    assert weights == {"AAA.SZ": 0.25}


def test_snapshot_same_day_overwrite_and_previous(
    home: Path,
) -> None:
    snap1 = MonitorDaySnapshot(
        as_of="2026-07-24",
        scanned_at="2026-07-24T07:00:00+00:00",
        run_id="run_abc",
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.1, action="enter")],
        ops=DayOps(entered=["AAA.SZ"]),
    )
    save_day_snapshot(snap1)

    snap2 = MonitorDaySnapshot(
        as_of="2026-07-25",
        scanned_at="2026-07-25T01:00:00+00:00",
        run_id="run_abc",
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.1, action="hold")],
        ops=DayOps(held=["AAA.SZ"]),
    )
    save_day_snapshot(snap2)

    # Same-day overwrite keeps a single file for the date.
    snap2b = MonitorDaySnapshot(
        as_of="2026-07-25",
        scanned_at="2026-07-25T08:00:00+00:00",
        run_id="run_abc",
        holdings=[
            HoldingRow(symbol="AAA.SZ", weight=0.1, action="hold"),
            HoldingRow(symbol="BBB.SZ", weight=0.1, action="enter"),
        ],
        ops=DayOps(entered=["BBB.SZ"], held=["AAA.SZ"]),
    )
    save_day_snapshot(snap2b)

    loaded = load_day_snapshot("run_abc", "2026-07-25")
    assert loaded is not None
    assert loaded.scanned_at.endswith("08:00:00+00:00")
    assert len(loaded.holdings) == 2

    prev = load_previous_snapshot("run_abc", "2026-07-25")
    assert prev is not None
    assert prev.as_of == "2026-07-24"

    summaries = list_snapshot_summaries("run_abc", limit=10)
    assert [s["as_of"] for s in summaries] == ["2026-07-25", "2026-07-24"]


def test_scan_persists_holdings_from_full_universe(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "20260101_000000_00_hist01"
    run_dir = tmp_path / "runs" / run_id
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        out = {}",
                "        for code, df in data_map.items():",
                "            s = pd.Series(0.0, index=df.index)",
                "            # Hold A/B; leave C flat.",
                "            if code in ('AAA.SZ', 'BBB.SZ') and len(s):",
                "                s.iloc[-1] = 0.5",
                "            out[code] = s",
                "        return out",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "source": "auto",
                "interval": "1D",
                "codes": ["AAA.SZ", "BBB.SZ", "CCC.SZ"],
            }
        ),
        encoding="utf-8",
    )

    idx = pd.date_range("2024-06-01", periods=5, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000_000,
        },
        index=idx,
    )

    def fake_fetch_universe(universe, *, start, end, source, interval):
        return {symbol: frame.copy() for symbol in universe}, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)
    # Skip live-quote overlay so tests stay offline-deterministic.
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda data_map, *, as_of_date: (
            data_map,
            {k: False for k in data_map},
            {},
        ),
    )

    # Watchlist is a subset; holdings must still include the full Top book.
    cfg = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    day1 = run_monitor_scan(cfg, as_of=date(2024, 6, 7), runs_root=tmp_path / "runs")
    assert {h.symbol for h in day1.holdings} == {"AAA.SZ", "BBB.SZ"}
    assert set(day1.ops.entered) == {"AAA.SZ", "BBB.SZ"}
    assert [s.symbol for s in day1.suggestions] == ["AAA.SZ"]

    snap = load_day_snapshot(run_id, "2024-06-07")
    assert snap is not None
    assert snap.universe_size == 3

    # Second day: drop BBB, keep AAA → exit + hold.
    (code_dir / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        out = {}",
                "        for code, df in data_map.items():",
                "            s = pd.Series(0.0, index=df.index)",
                "            if code == 'AAA.SZ' and len(s):",
                "                s.iloc[-1] = 0.5",
                "            out[code] = s",
                "        return out",
            ]
        ),
        encoding="utf-8",
    )
    day2 = run_monitor_scan(cfg, as_of=date(2024, 6, 10), runs_root=tmp_path / "runs")
    assert {h.symbol for h in day2.holdings} == {"AAA.SZ"}
    assert day2.ops.held == ["AAA.SZ"]
    assert day2.ops.exited == ["BBB.SZ"]


def test_same_day_rescan_baselines_prior_day_not_morning_exit(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Morning exit must not stick: afternoon rescan diffs vs prior day only.

    Simulates open-print exit of BBB, then a later same-day scan where BBB is
    back in the book — ops/holdings must recalculate from the prior day's
    holdings, not from the morning snapshot that already dropped BBB.
    """
    run_id = "20260101_000000_00_sameday"
    run_dir = tmp_path / "runs" / run_id
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True)
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "source": "auto",
                "interval": "1D",
                "codes": ["AAA.SZ", "BBB.SZ"],
            }
        ),
        encoding="utf-8",
    )

    idx = pd.date_range("2024-06-01", periods=5, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000_000,
        },
        index=idx,
    )

    def fake_fetch_universe(universe, *, start, end, source, interval):
        return {symbol: frame.copy() for symbol in universe}, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda data_map, *, as_of_date: (
            data_map,
            {k: False for k in data_map},
            {},
        ),
    )

    def _write_engine(hold_bbb: bool) -> None:
        bbb_line = "                s.iloc[-1] = 0.5" if hold_bbb else "                pass"
        (code_dir / "signal_engine.py").write_text(
            "\n".join(
                [
                    "import pandas as pd",
                    "class SignalEngine:",
                    "    def generate(self, data_map):",
                    "        out = {}",
                    "        for code, df in data_map.items():",
                    "            s = pd.Series(0.0, index=df.index)",
                    "            if code == 'AAA.SZ' and len(s):",
                    "                s.iloc[-1] = 0.5",
                    "            if code == 'BBB.SZ' and len(s):",
                    bbb_line,
                    "            out[code] = s",
                    "        return out",
                ]
            ),
            encoding="utf-8",
        )

    cfg = MonitorConfig(
        symbols=["AAA.SZ", "BBB.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )

    # Prior day: both names on the book.
    _write_engine(hold_bbb=True)
    prior = run_monitor_scan(cfg, as_of=date(2024, 6, 7), runs_root=tmp_path / "runs")
    assert {h.symbol for h in prior.holdings} == {"AAA.SZ", "BBB.SZ"}

    # Morning same-day: BBB drops → exit vs prior day.
    _write_engine(hold_bbb=False)
    morning = run_monitor_scan(cfg, as_of=date(2024, 6, 10), runs_root=tmp_path / "runs")
    assert {h.symbol for h in morning.holdings} == {"AAA.SZ"}
    assert morning.ops.exited == ["BBB.SZ"]
    assert morning.ops.held == ["AAA.SZ"]

    morning_snap = load_day_snapshot(run_id, "2024-06-10")
    assert morning_snap is not None
    assert {h.symbol for h in morning_snap.holdings} == {"AAA.SZ"}

    # Afternoon: BBB recovers. Must still baseline prior day (both on),
    # not the morning snapshot — so BBB is hold, not a fresh enter, and
    # not stuck as exited.
    _write_engine(hold_bbb=True)
    afternoon = run_monitor_scan(cfg, as_of=date(2024, 6, 10), runs_root=tmp_path / "runs")
    assert {h.symbol for h in afternoon.holdings} == {"AAA.SZ", "BBB.SZ"}
    assert set(afternoon.ops.held) == {"AAA.SZ", "BBB.SZ"}
    assert afternoon.ops.exited == []
    assert afternoon.ops.entered == []

    # History file overwritten with the latest live recalculation.
    latest = load_day_snapshot(run_id, "2024-06-10")
    assert latest is not None
    assert {h.symbol for h in latest.holdings} == {"AAA.SZ", "BBB.SZ"}


def test_history_routes(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    from src.monitor.store import save_monitor

    save_monitor(
        MonitorConfig(strategy=StrategyRef(type="run_dir", run_id="run_hist_route"))
    )
    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2026-07-20",
            scanned_at="2026-07-20T08:00:00+00:00",
            run_id="run_hist_route",
            holdings=[HoldingRow(symbol="AAA.SZ", weight=0.2, action="enter")],
            ops=DayOps(entered=["AAA.SZ"]),
            universe_size=10,
        )
    )

    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    listed = client.get("/monitor/history?limit=10")
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["as_of"] == "2026-07-20"
    assert body[0]["holding_count"] == 1

    detail = client.get("/monitor/history/2026-07-20")
    assert detail.status_code == 200
    assert detail.json()["holdings"][0]["symbol"] == "AAA.SZ"

    missing = client.get("/monitor/history/2026-01-01")
    assert missing.status_code == 404
