"""PATCH /runs/{run_id} renames the display title used by Monitor/Reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.session.models import Session
from src.session.store import SessionStore


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    runs_dir.mkdir()
    sessions_dir.mkdir()
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(api_server, "SESSIONS_DIR", sessions_dir)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _write_run(
    runs_dir: Path,
    run_id: str,
    prompt: str,
    session_id: str | None = None,
) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir()
    payload: dict = {"prompt": prompt, "context": {}}
    if session_id:
        payload["context"]["session_id"] = session_id
    (run_dir / "req.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run_dir


def test_rename_run_persists_title_for_orphan(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_id = "20260726_090000_01_orphan"
    _write_run(runs_dir, run_id, "很长的原始 prompt 不该被改掉")

    response = client.patch(f"/runs/{run_id}", json={"title": "  工业母机 F1  "})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "updated"
    assert body["run_id"] == run_id
    assert body["title"] == "工业母机 F1"

    req = json.loads((runs_dir / run_id / "req.json").read_text(encoding="utf-8"))
    assert req["title"] == "工业母机 F1"
    assert req["prompt"] == "很长的原始 prompt 不该被改掉"

    listed = client.get("/runs").json()
    assert listed[0]["title"] == "工业母机 F1"
    assert listed[0]["prompt"] == "很长的原始 prompt 不该被改掉"


def test_rename_run_syncs_linked_session_title(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    session_id = "sess_monitor_rename"
    store = SessionStore(base_dir=sessions_dir)
    store.create_session(Session(session_id=session_id, title="旧标题"))

    run_id = "20260726_090100_01_linked"
    _write_run(runs_dir, run_id, "原始 prompt", session_id=session_id)

    response = client.patch(f"/runs/{run_id}", json={"title": "国泰君安 F0 机械"})
    assert response.status_code == 200

    session = store.get_session(session_id)
    assert session is not None
    assert session.title == "国泰君安 F0 机械"
    assert client.get("/runs").json()[0]["title"] == "国泰君安 F0 机械"


def test_rename_run_rejects_empty_title(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_id = "20260726_090200_01_empty"
    _write_run(runs_dir, run_id, "prompt")

    response = client.patch(f"/runs/{run_id}", json={"title": "   "})
    assert response.status_code == 400


def test_rename_run_missing_returns_404(client: TestClient) -> None:
    response = client.patch("/runs/20260726_090300_01_missing", json={"title": "x"})
    assert response.status_code == 404
