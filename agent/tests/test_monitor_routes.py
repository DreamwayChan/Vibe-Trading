"""Route-level contracts for GET/PUT /monitor and POST /monitor/scan."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import monitor_routes, scheduled_routes
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.store import save_monitor
from src.scheduled_research.store import ScheduledResearchJobStore


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScheduledResearchJobStore:
    isolated = ScheduledResearchJobStore(path=tmp_path / "scheduled_jobs.json")
    monkeypatch.setattr(scheduled_routes, "_scheduled_research_store", isolated)
    return isolated


@pytest.fixture
def client(home: Path, store: ScheduledResearchJobStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_get_monitor_empty_default(client: TestClient) -> None:
    response = client.get("/monitor")
    assert response.status_code == 200
    body = response.json()
    assert body["symbols"] == []
    assert body["suggestions"] == []


def test_put_monitor_persists_symbols_and_schedule(
    client: TestClient, store: ScheduledResearchJobStore
) -> None:
    response = client.put(
        "/monitor",
        json={
            "symbols": ["600519.SH", "000858.SZ"],
            "strategy": {"type": "run_dir", "run_id": None},
            "schedule": "30 7 * * *",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbols"] == ["600519.SH", "000858.SZ"]
    assert body["schedule"] == "30 7 * * *"

    job = store.get(monitor_routes.MONITOR_SCHEDULE_JOB_ID)
    assert job is not None
    assert job.config.get("job_type") == "monitor_scan"

    # Clearing schedule removes the job.
    cleared = client.put("/monitor", json={"schedule": ""})
    assert cleared.status_code == 200
    assert cleared.json()["schedule"] is None
    assert store.get(monitor_routes.MONITOR_SCHEDULE_JOB_ID) is None


def test_scan_endpoint_uses_run_monitor_scan(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            symbols=["AAA"],
            strategy=StrategyRef(type="run_dir", run_id="fake"),
        )
    )

    seen: dict[str, object] = {}

    def fake_scan(config, **kwargs):
        seen["refresh_report"] = kwargs.get("refresh_report")
        config.suggestions = [
            MonitorSuggestion(
                symbol="AAA",
                action="buy",
                reason="crossed into long",
                as_of="2024-01-02",
            )
        ]
        config.last_scan_at = "2024-01-02T00:00:00+00:00"
        return config

    monkeypatch.setattr(monitor_routes, "run_monitor_scan", fake_scan)
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)

    response = client.post("/monitor/scan")
    assert response.status_code == 200
    body = response.json()
    assert body["suggestions"][0]["action"] == "buy"
    assert body["last_scan_at"]
    # Empty/default body must keep legacy scan-only behaviour.
    assert seen.get("refresh_report") is False


def test_scan_endpoint_passes_refresh_report_flag(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            symbols=["AAA"],
            strategy=StrategyRef(type="run_dir", run_id="fake"),
        )
    )
    seen: dict[str, object] = {}

    def fake_scan(config, **kwargs):
        seen["refresh_report"] = kwargs.get("refresh_report")
        config.last_scan_at = "2024-01-02T00:00:00+00:00"
        return config

    monkeypatch.setattr(monitor_routes, "run_monitor_scan", fake_scan)
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)

    response = client.post("/monitor/scan", json={"refresh_report": True})
    assert response.status_code == 200
    assert seen.get("refresh_report") is True


def test_monitor_holdings_include_trend_action(
    client: TestClient, home: Path
) -> None:
    """Tape action must survive GET /monitor — Pydantic used to drop ``trend``."""
    from src.monitor.models import DayOps, HoldingRow, MonitorDaySnapshot
    from src.monitor.store import save_day_snapshot

    trend = {
        "market_regime": "chop",
        "stock_regime": "bull",
        "swing": "mid",
        "action": "hold",
        "fade_rallies": False,
        "reason": "index chop + relative-strength bull",
    }
    save_monitor(
        MonitorConfig(
            symbols=["002415.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_memory"),
            last_scan_at="2026-08-24T00:00:00+00:00",
            holdings=[
                HoldingRow(
                    symbol="002415.SZ",
                    weight=0.16,
                    action="hold",
                    price=32.1,
                    data_as_of="2026-08-24",
                    trend=trend,
                )
            ],
            ops=DayOps(held=["002415.SZ"]),
        )
    )
    listed = client.get("/monitor")
    assert listed.status_code == 200
    row = listed.json()["holdings"][0]
    assert row["action"] == "hold"
    assert row["trend"]["action"] == "hold"
    assert row["trend"]["stock_regime"] == "bull"

    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2026-08-24",
            scanned_at="2026-08-24T00:00:00+00:00",
            run_id="run_memory",
            holdings=[
                HoldingRow(
                    symbol="002415.SZ",
                    weight=0.16,
                    action="hold",
                    trend=trend,
                )
            ],
            ops=DayOps(held=["002415.SZ"]),
        )
    )
    detail = client.get("/monitor/history/2026-08-24")
    assert detail.status_code == 200
    assert detail.json()["holdings"][0]["trend"]["action"] == "hold"


def test_put_switches_strategy_board_without_copying_symbols(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing strategy source loads that board; previous symbols stay on the old board."""
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)

    first = client.put(
        "/monitor",
        json={
            "symbols": ["AAA.SZ"],
            "strategy": {"type": "run_dir", "run_id": "run_a"},
        },
    )
    assert first.status_code == 200
    assert first.json()["symbols"] == ["AAA.SZ"]

    # Seed a scan cache on board A so we can prove it is restored later.
    save_monitor(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_a"),
            suggestions=[
                MonitorSuggestion(
                    symbol="AAA.SZ",
                    action="buy",
                    reason="cached A",
                    as_of="2024-01-02",
                )
            ],
            last_scan_at="2024-01-02T00:00:00+00:00",
        )
    )

    second = client.put(
        "/monitor",
        json={
            "symbols": ["BBB.SZ"],
            "strategy": {"type": "run_dir", "run_id": "run_b"},
        },
    )
    assert second.status_code == 200
    body_b = second.json()
    assert body_b["strategy"]["run_id"] == "run_b"
    assert body_b["symbols"] == ["BBB.SZ"]
    assert body_b["suggestions"] == []

    # Switch back with strategy only — must restore A's watchlist + suggestions.
    back = client.put(
        "/monitor",
        json={"strategy": {"type": "run_dir", "run_id": "run_a"}},
    )
    assert back.status_code == 200
    body_a = back.json()
    assert body_a["strategy"]["run_id"] == "run_a"
    assert body_a["symbols"] == ["AAA.SZ"]
    assert body_a["suggestions"][0]["reason"] == "cached A"

    # Board B must remain intact.
    again_b = client.put(
        "/monitor",
        json={"strategy": {"type": "run_dir", "run_id": "run_b"}},
    )
    assert again_b.json()["symbols"] == ["BBB.SZ"]
    assert again_b.json()["suggestions"] == []


def test_reset_clears_history_and_rescans(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /monitor/reset wipes snapshots then runs a fresh day-one scan."""
    from src.monitor.models import DayOps, HoldingRow, MonitorDaySnapshot
    from src.monitor.store import list_snapshot_dates, save_day_snapshot

    save_monitor(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_reset_me"),
            last_scan_at="2024-01-02T00:00:00+00:00",
            suggestions=[
                MonitorSuggestion(
                    symbol="AAA.SZ",
                    action="hold",
                    reason="stale",
                    as_of="2024-01-02",
                )
            ],
            holdings=[HoldingRow(symbol="AAA.SZ", weight=0.1, action="hold")],
            ops=DayOps(held=["AAA.SZ"]),
        )
    )
    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2024-01-01",
            scanned_at="2024-01-01T00:00:00+00:00",
            run_id="run_reset_me",
            holdings=[HoldingRow(symbol="AAA.SZ", weight=0.1, action="enter")],
            ops=DayOps(entered=["AAA.SZ"]),
        )
    )
    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2024-01-02",
            scanned_at="2024-01-02T00:00:00+00:00",
            run_id="run_reset_me",
            holdings=[HoldingRow(symbol="AAA.SZ", weight=0.1, action="hold")],
            ops=DayOps(held=["AAA.SZ"]),
        )
    )
    assert list_snapshot_dates("run_reset_me") == ["2024-01-01", "2024-01-02"]

    def fake_scan(config, **_kwargs):
        # After reset there is no prior day → first-scan enter semantics.
        config.suggestions = [
            MonitorSuggestion(
                symbol="AAA.SZ",
                action="buy",
                reason="fresh day-one",
                as_of="2024-01-03",
            )
        ]
        config.holdings = [HoldingRow(symbol="AAA.SZ", weight=0.2, action="enter")]
        config.ops = DayOps(entered=["AAA.SZ"])
        config.last_scan_at = "2024-01-03T00:00:00+00:00"
        return config

    monkeypatch.setattr(monitor_routes, "run_monitor_scan", fake_scan)
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)

    response = client.post("/monitor/reset")
    assert response.status_code == 200
    body = response.json()
    assert body["symbols"] == ["AAA.SZ"]
    assert body["suggestions"][0]["reason"] == "fresh day-one"
    assert body["holdings"][0]["action"] == "enter"
    assert body["ops"]["entered"] == ["AAA.SZ"]
    assert body["last_scan_at"]
    # Prior snapshots gone; only whatever the fake scan may have written (none).
    assert list_snapshot_dates("run_reset_me") == []


def test_clear_strategy_history_helper(home: Path) -> None:
    from src.monitor.models import DayOps, HoldingRow, MonitorDaySnapshot
    from src.monitor.store import (
        clear_strategy_history,
        list_snapshot_dates,
        save_day_snapshot,
    )

    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2026-07-20",
            scanned_at="2026-07-20T08:00:00+00:00",
            run_id="run_clear_hist",
            holdings=[HoldingRow(symbol="AAA.SZ", weight=0.2, action="enter")],
            ops=DayOps(entered=["AAA.SZ"]),
        )
    )
    assert clear_strategy_history("run_clear_hist") is True
    assert list_snapshot_dates("run_clear_hist") == []
    # Idempotent when already empty.
    assert clear_strategy_history("run_clear_hist") is False


def test_forecast_endpoint_uses_run_monitor_forecast(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="fake"),
            holdings=[],
        )
    )
    seen: dict[str, object] = {}

    def fake_forecast(config, **kwargs):
        seen["as_of"] = kwargs.get("as_of")
        return {
            "as_of": "2024-01-02",
            "horizon": 10,
            "holding_count": 1,
            "evaluated_count": 1,
            "capped": False,
            "bias": "up",
            "items": [
                {
                    "symbol": "AAA.SZ",
                    "weight": 0.2,
                    "price": 10.0,
                    "up_prob": 0.62,
                    "down_prob": 0.38,
                    "style_score": 0.7,
                    "assessment": {
                        "bias": "up",
                        "style_bucket": "high",
                        "ma_structure": "bull",
                        "analog_count": 12,
                        "up_count": 8,
                        "down_count": 4,
                    },
                    "history": [
                        {
                            "time": "2024-01-02",
                            "open": 10.0,
                            "high": 10.2,
                            "low": 9.9,
                            "close": 10.1,
                            "volume": 100.0,
                        }
                    ],
                    "up_bars": [
                        {
                            "time": "2024-01-03",
                            "open": 10.1,
                            "high": 10.4,
                            "low": 10.0,
                            "close": 10.3,
                            "volume": 0.0,
                        }
                    ]
                    * 10,
                    "down_bars": [
                        {
                            "time": "2024-01-03",
                            "open": 10.1,
                            "high": 10.1,
                            "low": 9.7,
                            "close": 9.8,
                            "volume": 0.0,
                        }
                    ]
                    * 10,
                    "ma": {
                        "history": {"ma5": [10.0], "ma10": [None], "ma20": [None]},
                        "up": {"ma5": [10.2] * 10, "ma10": [10.1] * 10, "ma20": [10.0] * 10},
                        "down": {"ma5": [9.9] * 10, "ma10": [10.0] * 10, "ma20": [10.0] * 10},
                    },
                    "error": None,
                }
            ],
        }

    monkeypatch.setattr(monitor_routes, "run_monitor_forecast", fake_forecast)
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)

    response = client.post("/monitor/forecast", json={"as_of": "2024-01-02"})
    assert response.status_code == 200
    body = response.json()
    assert body["bias"] == "up"
    assert body["items"][0]["symbol"] == "AAA.SZ"
    assert len(body["items"][0]["up_bars"]) == 10
    assert seen.get("as_of") == "2024-01-02"


def test_forecast_endpoint_rejects_bad_strategy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            strategy=StrategyRef(type="run_dir", run_id="missing"),
        )
    )

    def boom(*_a, **_k):
        from src.monitor.scan import MonitorScanError

        raise MonitorScanError("strategy.run_id is required")

    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", boom)
    response = client.post("/monitor/forecast", json={})
    assert response.status_code == 400


def _sample_forecast_body() -> dict:
    bar = {
        "time": "2024-01-03",
        "open": 10.1,
        "high": 10.4,
        "low": 10.0,
        "close": 10.3,
        "volume": 0.0,
    }
    return {
        "as_of": "2024-01-02",
        "horizon": 10,
        "holding_count": 1,
        "evaluated_count": 1,
        "capped": False,
        "bias": "up",
        "items": [
            {
                "symbol": "AAA.SZ",
                "weight": 0.2,
                "price": 10.0,
                "up_prob": 0.62,
                "down_prob": 0.38,
                "style_score": 0.7,
                "assessment": {
                    "bias": "up",
                    "style_bucket": "high",
                    "ma_structure": "bull",
                    "analog_count": 12,
                    "up_count": 8,
                    "down_count": 4,
                },
                "history": [
                    {
                        "time": "2024-01-02",
                        "open": 10.0,
                        "high": 10.2,
                        "low": 9.9,
                        "close": 10.1,
                        "volume": 100.0,
                    }
                ],
                "up_bars": [bar] * 10,
                "down_bars": [bar] * 10,
                "ma": {
                    "history": {"ma5": [10.0], "ma10": [None], "ma20": [None]},
                    "up": {"ma5": [10.2] * 10, "ma10": [10.1] * 10, "ma20": [10.0] * 10},
                    "down": {"ma5": [9.9] * 10, "ma10": [10.0] * 10, "ma20": [10.0] * 10},
                },
                "error": None,
            }
        ],
    }


def test_forecast_save_list_and_reload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_forecast_save"),
        )
    )
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)
    created = client.post("/monitor/forecast/saves", json=_sample_forecast_body())
    assert created.status_code == 200
    body = created.json()
    assert body["id"]
    assert body["items"][0]["symbol"] == "AAA.SZ"

    listed = client.get("/monitor/forecast/saves")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert "items" not in rows[0]

    loaded = client.get(f"/monitor/forecast/saves/{body['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["items"][0]["symbol"] == "AAA.SZ"


def test_forecast_save_rejects_empty_items(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_monitor(
        MonitorConfig(
            strategy=StrategyRef(type="run_dir", run_id="run_forecast_save_empty"),
        )
    )
    monkeypatch.setattr(monitor_routes, "validate_strategy_ref", lambda *_a, **_k: None)
    response = client.post(
        "/monitor/forecast/saves",
        json={
            "as_of": "2024-01-02",
            "horizon": 10,
            "holding_count": 0,
            "evaluated_count": 0,
            "capped": False,
            "bias": "neutral",
            "items": [],
        },
    )
    assert response.status_code == 400
