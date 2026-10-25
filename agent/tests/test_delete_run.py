"""DELETE /runs/{run_id} removes the run dir and linked monitor artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.monitor.models import HoldingRow, MonitorConfig, MonitorDaySnapshot, StrategyRef
from src.monitor.store import (
    board_path,
    delete_board_and_history_for_run,
    get_active_board_id,
    history_dir,
    save_day_snapshot,
    save_monitor,
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _seed_run(runs_dir: Path, run_id: str) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir()
    (run_dir / "req.json").write_text(
        json.dumps({"prompt": "demo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "state.json").write_text(
        json.dumps({"status": "success"}),
        encoding="utf-8",
    )
    return run_dir


def test_delete_run_removes_directory_and_monitor_board(
    client: TestClient, tmp_path: Path
) -> None:
    runs_dir = tmp_path / "runs"
    run_id = "20260725_120000_01_demo"
    run_dir = _seed_run(runs_dir, run_id)

    save_monitor(
        MonitorConfig(
            symbols=["600519.SH"],
            strategy=StrategyRef(type="run_dir", run_id=run_id),
        )
    )
    save_day_snapshot(
        MonitorDaySnapshot(
            as_of="2026-07-25",
            scanned_at="2026-07-25T07:30:00+00:00",
            run_id=run_id,
            holdings=[HoldingRow(symbol="600519.SH", weight=0.1, action="enter")],
        )
    )
    assert board_path(f"run_{run_id}").is_file()
    assert history_dir(run_id).is_dir()
    assert get_active_board_id() == f"run_{run_id}"

    response = client.delete(f"/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deleted"
    assert body["run_id"] == run_id
    assert body["monitor_cleanup"]["board"] is True
    assert body["monitor_cleanup"]["history"] is True
    assert body["monitor_cleanup"]["cleared_active"] is True

    assert not run_dir.exists()
    assert not board_path(f"run_{run_id}").exists()
    assert not (tmp_path / ".vibe-trading" / "monitor" / "history" / run_id).exists()
    assert get_active_board_id() == "draft"
    assert client.get("/runs").json() == []


def test_delete_run_missing_returns_404(client: TestClient) -> None:
    response = client.delete("/runs/20260725_120000_01_missing")
    assert response.status_code == 404


def test_delete_run_rejects_unsafe_id(client: TestClient) -> None:
    response = client.delete("/runs/foo.bar")
    assert response.status_code == 400


def test_delete_board_and_history_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cleanup = delete_board_and_history_for_run("20260725_120000_01_gone")
    assert cleanup == {"board": False, "history": False, "cleared_active": False}
