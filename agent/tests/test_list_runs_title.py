"""list_runs should surface the live session title when a chat is renamed."""

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
    session_id: str | None,
    *,
    metrics: dict[str, float] | None = None,
) -> None:
    run_dir = runs_dir / run_id
    run_dir.mkdir()
    payload: dict = {"prompt": prompt, "context": {}}
    if session_id:
        payload["context"]["session_id"] = session_id
    (run_dir / "req.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if metrics:
        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        keys = list(metrics.keys())
        (artifacts / "metrics.csv").write_text(
            ",".join(keys)
            + "\n"
            + ",".join(str(metrics[k]) for k in keys)
            + "\n",
            encoding="utf-8",
        )


def test_list_runs_prefers_renamed_session_title(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    sessions_dir = tmp_path / "sessions"
    session_id = "sess_rename_1"
    store = SessionStore(base_dir=sessions_dir)
    session = Session(session_id=session_id, title="原始标题截断")
    store.create_session(session)
    # Sidebar rename only mutates session.title — prompt in req.json stays frozen.
    session.title = "国泰君安多因子C方案"
    store.update_session(session)

    _write_run(
        runs_dir,
        "20260725_094658_01_531d4f",
        "请用国泰君安 191 做多因子选股并回测……很长的原始 prompt",
        session_id,
    )

    response = client.get("/runs")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "国泰君安多因子C方案"
    # Immutable prompt snapshot is still returned for detail/debug use.
    assert body[0]["prompt"].startswith("请用国泰君安")


def test_list_runs_falls_back_to_prompt_without_session(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_run(runs_dir, "20260725_100000_00_abcdef", "Manual orphan run prompt", None)

    response = client.get("/runs")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["title"] == "Manual orphan run prompt"
    assert body[0]["prompt"] == "Manual orphan run prompt"


def test_list_runs_uses_stored_req_title_for_orphan(client: TestClient, tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_id = "20260726_101000_00_titled"
    run_dir = runs_dir / run_id
    run_dir.mkdir()
    (run_dir / "req.json").write_text(
        json.dumps(
            {"prompt": "原始长 prompt", "title": "工业母机 F1", "context": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get("/runs")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["title"] == "工业母机 F1"
    assert row["prompt"] == "原始长 prompt"


def test_list_runs_includes_return_metrics_for_disambiguation(
    client: TestClient, tmp_path: Path
) -> None:
    runs_dir = tmp_path / "runs"
    _write_run(
        runs_dir,
        "20260725_170000_pharma_d_absorb",
        "创新药 D 吸筹",
        None,
        metrics={
            "total_return": 0.0935,
            "excess_return": -0.2585,
            "max_drawdown": -0.27,
            "sharpe": 0.284,
        },
    )

    response = client.get("/runs")
    assert response.status_code == 200
    row = response.json()[0]
    assert row["total_return"] == pytest.approx(0.0935)
    assert row["excess_return"] == pytest.approx(-0.2585)
    assert row["max_drawdown"] == pytest.approx(-0.27)
    assert row["sharpe"] == pytest.approx(0.284)
    assert row["has_signal_engine"] is False


def test_list_runs_strategy_ready_filters_chat_only_runs(
    client: TestClient, tmp_path: Path
) -> None:
    """Monitor must not offer WeChat/Q&A run dirs that lack signal_engine.py."""
    runs_dir = tmp_path / "runs"
    chat_id = "20260727_132619_15_ad36af"
    backtest_id = "20260726_100000_01_bt001"

    _write_run(runs_dir, chat_id, "微信上随便问问行情", None)
    _write_run(runs_dir, backtest_id, "双均线回测", None)
    engine = runs_dir / backtest_id / "code"
    engine.mkdir()
    (engine / "signal_engine.py").write_text(
        "class SignalEngine:\n    pass\n", encoding="utf-8"
    )

    all_runs = client.get("/runs").json()
    assert {row["run_id"] for row in all_runs} == {chat_id, backtest_id}
    by_id = {row["run_id"]: row for row in all_runs}
    assert by_id[chat_id]["has_signal_engine"] is False
    assert by_id[backtest_id]["has_signal_engine"] is True

    ready = client.get("/runs", params={"strategy_ready": "true"}).json()
    assert [row["run_id"] for row in ready] == [backtest_id]
    assert ready[0]["has_signal_engine"] is True
