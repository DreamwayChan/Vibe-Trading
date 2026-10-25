"""Route-level contracts for GET /market-pick and POST /market-pick/scan."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.market_pick.models import BoardScore, MarketPickResult
from src.market_pick.store import save_day_snapshot


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def client(home: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_get_market_pick_empty(client: TestClient) -> None:
    response = client.get("/market-pick")
    assert response.status_code == 200
    body = response.json()
    assert body["result"] is None
    assert body["cached"] is True


def test_get_does_not_run_scan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise AssertionError("GET must not scan")

    monkeypatch.setattr("src.api.market_pick_routes.run_market_pick_scan", boom)
    response = client.get("/market-pick")
    assert response.status_code == 200
    assert response.json()["result"] is None


def test_get_returns_saved_snapshot(client: TestClient, home: Path) -> None:
    save_day_snapshot(
        MarketPickResult(
            as_of="2024-01-02",
            scanned_at="2024-01-02T00:00:00+00:00",
            market_regime="chop",
            stance="full",
            reason="金属铜：强势",
            recommended_bk="BK1615",
            unique_symbols=12,
            ohlcv_ok=10,
            boards=[
                BoardScore(
                    bk="BK1615",
                    name="金属铜",
                    board_type="industry",
                    classified=20,
                    rs_share=0.5,
                    knife_share=0.1,
                    catalog_key="copper",
                    catalog_name="金属铜",
                    rank=1,
                )
            ],
        )
    )
    response = client.get("/market-pick")
    assert response.status_code == 200
    row = response.json()["result"]
    assert row["recommended_bk"] == "BK1615"
    assert row["boards"][0]["catalog_key"] == "copper"


def test_post_scan_blocked_without_force(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date.today().isoformat()
    save_day_snapshot(
        MarketPickResult(as_of=today, scanned_at=f"{today}T00:00:00+00:00", reason="cached")
    )

    def boom():
        raise AssertionError("must not scan when today's snapshot exists")

    monkeypatch.setattr("src.api.market_pick_routes.run_market_pick_scan", boom)
    response = client.post("/market-pick/scan", json={"force": False})
    assert response.status_code == 409
    assert "force=true" in response.json()["detail"]


def test_post_scan_force_rebuilds(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today().isoformat()
    save_day_snapshot(
        MarketPickResult(as_of=today, scanned_at=f"{today}T00:00:00+00:00", reason="old")
    )
    fake = MarketPickResult(
        as_of=today,
        scanned_at=f"{today}T01:00:00+00:00",
        stance="none",
        reason="rebuilt",
        unique_symbols=3,
    )
    monkeypatch.setattr(
        "src.api.market_pick_routes.run_market_pick_scan", lambda: fake
    )
    response = client.post("/market-pick/scan", json={"force": True})
    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is False
    assert body["result"]["reason"] == "rebuilt"
    again = client.get("/market-pick")
    assert again.json()["result"]["reason"] == "rebuilt"


def test_history_lists_and_loads_day(client: TestClient, home: Path) -> None:
    save_day_snapshot(
        MarketPickResult(
            as_of="2024-01-01",
            scanned_at="2024-01-01T00:00:00+00:00",
            stance="none",
            reason="older",
        )
    )
    save_day_snapshot(
        MarketPickResult(
            as_of="2024-01-02",
            scanned_at="2024-01-02T00:00:00+00:00",
            market_regime="chop",
            stance="full",
            reason="金属铜：强势",
            recommended_bk="BK1615",
            unique_symbols=12,
            ohlcv_ok=10,
            boards=[
                BoardScore(
                    bk="BK1615",
                    name="金属铜",
                    board_type="industry",
                    classified=20,
                    rs_share=0.5,
                    knife_share=0.1,
                    catalog_key="copper",
                    catalog_name="金属铜",
                    rank=1,
                )
            ],
        )
    )
    listed = client.get("/market-pick/history")
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["as_of"] for row in rows] == ["2024-01-02", "2024-01-01"]
    assert rows[0]["recommended_bk"] == "BK1615"
    assert rows[0]["recommended_name"] == "金属铜"
    assert rows[0]["board_count"] == 1
    day = client.get("/market-pick/history/2024-01-02")
    assert day.status_code == 200
    body = day.json()
    assert body["recommended_bk"] == "BK1615"
    assert body["boards"][0]["catalog_key"] == "copper"
    latest = client.get("/market-pick")
    assert latest.json()["result"]["as_of"] == "2024-01-02"


def test_history_day_404(client: TestClient) -> None:
    response = client.get("/market-pick/history/1999-01-01")
    assert response.status_code == 404


def test_force_scan_overwrites_today_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = date.today().isoformat()
    save_day_snapshot(
        MarketPickResult(
            as_of=today,
            scanned_at=f"{today}T00:00:00+00:00",
            reason="morning",
            recommended_bk="BK0001",
        )
    )
    fake = MarketPickResult(
        as_of=today,
        scanned_at=f"{today}T08:00:00+00:00",
        stance="full",
        reason="afternoon",
        recommended_bk="BK1615",
        boards=[BoardScore(bk="BK1615", name="金属铜", board_type="industry")],
    )
    monkeypatch.setattr("src.api.market_pick_routes.run_market_pick_scan", lambda: fake)
    response = client.post("/market-pick/scan", json={"force": True})
    assert response.status_code == 200
    listed = client.get("/market-pick/history")
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["as_of"] == today
    assert rows[0]["recommended_name"] == "金属铜"
    day = client.get(f"/market-pick/history/{today}")
    assert day.json()["reason"] == "afternoon"
