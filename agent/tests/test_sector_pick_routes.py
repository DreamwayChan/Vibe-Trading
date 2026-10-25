"""Route-level contracts for GET/PUT /sector-pick and POST scan/holdings."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.monitor.models import DayOps, HoldingRow
from src.sector_pick.models import SectorPickResult, SectorScore
from src.sector_pick.store import load_state, save_state


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def client(home: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_get_sector_pick_defaults_to_full_catalog(client: TestClient) -> None:
    response = client.get("/sector-pick")
    assert response.status_code == 200
    body = response.json()
    assert len(body["catalog"]) == 21
    assert body["result"] is None
    assert "robot" in body["enabled_keys"]
    assert "broker" in body["enabled_keys"]
    robot = next(row for row in body["catalog"] if row["key"] == "robot")
    assert robot["name"] == "人形机器人"
    assert "run_title" in robot
    assert "run_missing" in robot


def test_put_rejects_unknown_key(client: TestClient) -> None:
    response = client.put("/sector-pick", json={"enabled_keys": ["robot", "nope"]})
    assert response.status_code == 400
    assert "unknown sector key" in response.json()["detail"]


def test_put_persists_enabled_keys(client: TestClient) -> None:
    response = client.put("/sector-pick", json={"enabled_keys": ["copper", "robot"]})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled_keys"] == ["robot", "copper"]
    again = client.get("/sector-pick")
    assert again.json()["enabled_keys"] == ["robot", "copper"]


def test_scan_endpoint_uses_run_sector_pick_scan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = SectorPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        recommended_key="robot",
        holdings_key="robot",
        locked_key="robot",
        map_key="robot",
        stance="full",
        reason="人形机器人：板块等权均价站上 MA20",
        sectors=[
            SectorScore(
                key="robot",
                name="人形机器人",
                panel="BK1184",
                run_id="run_robot",
                variant="f0_prime",
                timing_mult=1.0,
                ret_20d=0.05,
                rank=1,
            )
        ],
        holdings=[HoldingRow(symbol="000001.SZ", weight=0.16, action="enter")],
        ops=DayOps(entered=["000001.SZ"]),
        holdings_run_id="run_robot",
    )

    def _fake_scan(enabled_keys, **kwargs):
        assert "robot" in enabled_keys
        return fake

    monkeypatch.setattr(
        "src.api.sector_pick_routes.run_sector_pick_scan", _fake_scan
    )
    response = client.post("/sector-pick/scan", json={"enabled_keys": ["robot"]})
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["recommended_key"] == "robot"
    assert body["result"]["holdings"][0]["symbol"] == "000001.SZ"
    assert body["enabled_keys"] == ["robot"]
    listed = client.get("/sector-pick/history")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["as_of"] == "2024-01-02"
    assert rows[0]["recommended_key"] == "robot"
    day = client.get("/sector-pick/history/2024-01-02")
    assert day.status_code == 200
    assert day.json()["holdings"][0]["symbol"] == "000001.SZ"
    series = client.get("/sector-pick/history/series")
    assert series.status_code == 200
    days = series.json()["days"]
    assert len(days) == 1
    assert days[0]["as_of"] == "2024-01-02"
    assert days[0]["recommended_key"] == "robot"
    assert days[0]["sectors"][0]["rank"] == 1
    # Official scan persists the lock so the next scan is not rank-chasing.
    persisted = load_state()
    assert persisted.theme_lock is not None
    assert persisted.theme_lock["locked_key"] == "robot"


def test_scan_passes_persisted_theme_lock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []

    def _fake_scan(enabled_keys, **kwargs):
        seen.append(kwargs.get("theme_lock"))
        lock = kwargs.get("theme_lock")
        locked = getattr(lock, "locked_key", None) if lock is not None else None
        return SectorPickResult(
            as_of="2024-01-03",
            scanned_at="2024-01-03T00:00:00+00:00",
            recommended_key=locked or "copper",
            locked_key=locked or "copper",
            map_key="robot",
            stance="full",
            reason="lock",
        )

    monkeypatch.setattr("src.api.sector_pick_routes.run_sector_pick_scan", _fake_scan)
    state = load_state()
    state.enabled_keys = ["robot", "copper"]
    state.theme_lock = {"locked_key": "copper", "challenger_key": None, "challenger_days": 0}
    save_state(state)
    response = client.post("/sector-pick/scan")
    assert response.status_code == 200
    assert seen and getattr(seen[0], "locked_key", None) == "copper"
    assert response.json()["result"]["locked_key"] == "copper"
    assert response.json()["result"]["map_key"] == "robot"


def test_holdings_preview_requires_prior_scan(client: TestClient) -> None:
    response = client.post("/sector-pick/holdings", json={"key": "robot"})
    assert response.status_code == 400
    assert "scan sectors first" in response.json()["detail"]


def test_holdings_preview_swaps_book_keeps_winner(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state()
    state.enabled_keys = ["robot", "copper"]
    state.result = SectorPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        recommended_key="robot",
        holdings_key="robot",
        stance="full",
        reason="keep me",
        sectors=[
            SectorScore(
                key="robot", name="人形机器人", panel="x", run_id="r1", variant="f0_prime", rank=1
            ),
            SectorScore(
                key="copper", name="金属铜", panel="x", run_id="r2", variant="f0_prime", rank=2
            ),
        ],
        holdings=[HoldingRow(symbol="ROBOT.SZ", weight=0.16, action="hold")],
        holdings_run_id="r1",
    )
    save_state(state)

    def _fake_holdings(key, **kwargs):
        assert key == "copper"
        return (
            [HoldingRow(symbol="COPPER.SH", weight=0.2, action="enter")],
            DayOps(entered=["COPPER.SH"]),
            "run_copper",
            False,
        )

    monkeypatch.setattr(
        "src.api.sector_pick_routes.load_sector_holdings", _fake_holdings
    )
    response = client.post("/sector-pick/holdings", json={"key": "copper"})
    assert response.status_code == 200
    body = response.json()["result"]
    assert body["recommended_key"] == "robot"
    assert body["holdings_key"] == "copper"
    assert body["reason"] == "keep me"
    assert body["holdings"][0]["symbol"] == "COPPER.SH"
    copper_row = next(row for row in body["sectors"] if row["key"] == "copper")
    assert copper_row["book_healthy"] is False
    assert copper_row["holding_count"] == 1
    # Preview mutates the live board only — the day's official snapshot stays the winner.
    listed = client.get("/sector-pick/history")
    assert listed.json() == []


def test_history_day_404(client: TestClient) -> None:
    response = client.get("/sector-pick/history/1999-01-01")
    assert response.status_code == 404


def test_holdings_unknown_key(client: TestClient) -> None:
    response = client.post("/sector-pick/holdings", json={"key": "nope"})
    assert response.status_code == 400
