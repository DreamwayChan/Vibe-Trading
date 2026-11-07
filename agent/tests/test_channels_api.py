"""API contracts for IM channel runtime controls."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import api_server


class FakeSessionService:
    """SessionService placeholder for channel status tests."""


def _client(
    tmp_path: Path,
    monkeypatch,
    *,
    channels_config: dict[str, object] | None = None,
) -> TestClient:
    import src.channels.config as channel_config
    import src.channels.pairing.store as pairing_store

    config_path = tmp_path / "agent.json"
    config_path.write_text(
        json.dumps({"channels": channels_config or {}}),
        encoding="utf-8",
    )
    config = channel_config.load_channels_config(config_path)
    config.update(
        {
            "websocket": {"enabled": False, "allow_from": ["*"]},
            "telegram": {"enabled": False},
            "slack": {"enabled": True},
        }
    )

    monkeypatch.setattr(api_server, "_channel_runtime", None)
    monkeypatch.setattr(api_server, "_channel_bus", None)
    monkeypatch.setattr(api_server, "_channel_manager", None)
    monkeypatch.setattr(api_server, "_get_session_service", lambda: FakeSessionService())
    monkeypatch.setattr(channel_config, "load_channels_config", lambda: dict(config))
    monkeypatch.setattr(pairing_store, "_store_path", lambda: tmp_path / "pairing.json")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_channels_status_reports_all_configured_adapters(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get("/channels/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert payload["channels"]["websocket"]["configured"] is True
    assert payload["channels"]["telegram"]["enabled"] is False
    assert payload["channels"]["slack"]["enabled"] is True
    assert "available" in payload["channels"]["slack"]
    assert "reply_timeout_s" not in payload["channels"]
    assert "send_max_retries" not in payload["channels"]
    assert api_server._channel_runtime is not None
    assert api_server._channel_runtime.config.reply_timeout_s == 600.0


def test_channels_runtime_uses_configured_reply_timeout(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, channels_config={"reply_timeout_s": 42})

    response = client.get("/channels/status")

    assert response.status_code == 200
    assert api_server._channel_runtime is not None
    assert api_server._channel_runtime.config.reply_timeout_s == 42


def test_channels_start_stop_are_controlled_runtime_actions(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    start = client.post("/channels/start")
    stop = client.post("/channels/stop")

    assert start.status_code == 200
    assert start.json()["status"] == "started"
    assert start.json()["running"] is True
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"
    assert stop.json()["running"] is False


def test_channels_pairing_command_uses_shared_store(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/channels/pairing/command",
        json={"channel": "telegram", "command": "list"},
    )

    assert response.status_code == 200
    assert response.json()["channel"] == "telegram"
    assert "No pending pairing requests" in response.json()["reply"]


def test_channels_settings_expose_autostart(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    payload = client.get("/channels/settings").json()

    assert payload["auto_start"] is True
    assert "env_override" in payload
    assert "env_locked" in payload


def test_unknown_channel_lifecycle_returns_404(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post("/channels/does-not-exist/start")

    assert response.status_code == 404


def test_pairing_requests_can_be_listed_and_approved(tmp_path: Path, monkeypatch) -> None:
    from src.channels.pairing import generate_code

    client = _client(tmp_path, monkeypatch)
    code = generate_code("telegram", "user-9")

    pending = client.get("/channels/pairing/pending").json()["pending"]
    assert [item["code"] for item in pending] == [code]

    approved = client.post("/channels/pairing/approve", json={"code": code})
    assert approved.status_code == 200
    assert approved.json() == {
        "status": "approved",
        "channel": "telegram",
        "sender_id": "user-9",
    }

    assert client.get("/channels/pairing/pending").json()["pending"] == []


def test_binding_is_rejected_for_credential_only_channels(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post("/channels/telegram/auth/session")

    assert response.status_code == 400
    assert "does not support scanning" in response.json()["detail"]


def test_autostart_resolution_prefers_env_override(monkeypatch) -> None:
    from types import SimpleNamespace

    import src.channels.config as channel_config
    import src.config.accessor as accessor
    from src.api.channels_routes import channels_should_autostart

    def env(value):
        return SimpleNamespace(
            agent_tuning=SimpleNamespace(vibe_trading_channels_auto_start=value)
        )

    monkeypatch.setattr(accessor, "get_env_config", lambda: env(None))
    monkeypatch.setattr(channel_config, "load_channels_config", lambda: {"auto_start": True})
    assert channels_should_autostart() is True

    monkeypatch.setattr(channel_config, "load_channels_config", lambda: {"auto_start": False})
    assert channels_should_autostart() is False

    # An explicit env value wins in both directions.
    monkeypatch.setattr(accessor, "get_env_config", lambda: env(True))
    monkeypatch.setattr(channel_config, "load_channels_config", lambda: {"auto_start": False})
    assert channels_should_autostart() is True

    monkeypatch.setattr(accessor, "get_env_config", lambda: env(False))
    monkeypatch.setattr(channel_config, "load_channels_config", lambda: {"auto_start": True})
    assert channels_should_autostart() is False


def test_channel_logs_endpoint_returns_buffered_lines(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    empty = client.get("/channels/slack/logs?limit=10")
    assert empty.status_code == 200
    assert empty.json() == {"channel": "slack", "lines": []}

    client.get("/channels/status")  # builds the manager, which binds the buffer
    api_server._channel_manager.channel_logs  # noqa: B018 - attribute exists
    api_server._channel_manager._note(  # noqa: SLF001 - test writes into the buffer
        "slack", 30, "slack adapter failed to connect"
    )

    payload = client.get("/channels/slack/logs?limit=10").json()
    assert payload["channel"] == "slack"
    assert any("failed to connect" in line["message"] for line in payload["lines"])

    cleared = client.delete("/channels/slack/logs")
    assert cleared.status_code == 200
    assert client.get("/channels/slack/logs").json()["lines"] == []


def test_channel_logs_returns_404_for_unknown_channel(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    assert client.get("/channels/nope/logs").status_code == 404
