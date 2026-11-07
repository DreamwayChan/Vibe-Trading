"""Lifecycle, autostart, config-write and binding coverage for IM channels."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from src.channels.bus.queue import MessageBus
from src.channels.manager import (
    STATE_NEEDS_AUTH,
    STATE_RUNNING,
    ChannelManager,
)


class _BlockingChannel:
    """Adapter whose start() never returns, like a long-poll implementation."""

    name = "dummy"
    display_name = "Dummy"
    auth_kind = "credentials"
    supports_interactive_login = False
    send_progress = True
    send_tool_hints = False
    show_reasoning = True
    logger = logging.getLogger("src.channels.base.dummy")

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._running = False
        self.needs_auth = False
        self._release = asyncio.Event()

    async def start(self) -> None:
        self.started = True
        self._running = True
        await self._release.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._running = False
        self._release.set()

    @property
    def is_running(self) -> bool:
        return self._running

    async def send(self, msg: Any) -> None:
        return None


def test_start_all_does_not_block_on_long_running_adapter() -> None:
    async def scenario() -> None:
        manager = ChannelManager({"auto_start": True}, MessageBus())
        channel = _BlockingChannel()
        manager.channels["dummy"] = channel

        began = time.monotonic()
        await manager.start_all()
        elapsed = time.monotonic() - began

        assert channel.started is True
        assert elapsed < 3.0, f"start_all blocked for {elapsed:.1f}s"
        assert manager.state_of("dummy") == STATE_RUNNING

        await manager.stop_channel("dummy")
        assert channel.stopped is True

    asyncio.run(scenario())


def test_per_channel_autostart_is_respected_on_boot() -> None:
    async def scenario() -> None:
        manager = ChannelManager(
            {"auto_start": True, "dummy": {"enabled": True, "auto_start": False}},
            MessageBus(),
        )
        manager._status["dummy"] = {"enabled": True, "available": True}
        channel = _BlockingChannel()
        manager.channels["dummy"] = channel

        await manager.start_all(autostart_only=True)
        assert channel.started is False
        assert manager.auto_start_enabled("dummy") is False

        # An explicit start ignores auto_start.
        await manager.start_channel("dummy")
        assert channel.started is True
        await manager.stop_channel("dummy")

    asyncio.run(scenario())


def test_channel_state_flags_credential_problems() -> None:
    manager = ChannelManager({"websocket": {"enabled": False}}, MessageBus())
    manager._status["dummy"] = {"enabled": True, "available": True, "loaded": True}
    channel = _BlockingChannel()
    manager.channels["dummy"] = channel

    assert manager.state_of("dummy") != STATE_NEEDS_AUTH
    channel.needs_auth = True
    assert manager.get_status()["dummy"]["state"] == STATE_NEEDS_AUTH


def test_writer_preserves_unrelated_keys_and_backs_up(tmp_path: Path) -> None:
    from src.config import writer

    config_path = tmp_path / "agent.json"
    config_path.write_text(
        json.dumps(
            {
                "channels": {"weixin": {"enabled": True, "token": "old"}},
                "mcpServers": {"demo": {"command": "echo"}},
            }
        ),
        encoding="utf-8",
    )

    writer.set_channel_section(
        "weixin",
        {"enabled": True, "token": "", "allow_from": ["wxid_1"]},
        config_path=config_path,
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["channels"]["weixin"]["allow_from"] == ["wxid_1"]
    # Credentials never stay in the shareable config file.
    assert "token" not in data["channels"]["weixin"]
    assert data["mcpServers"] == {"demo": {"command": "echo"}}
    assert list(tmp_path.glob("agent.json.bak.*")), "expected a backup of the previous file"


def test_credentials_are_split_into_the_secret_store(tmp_path: Path) -> None:
    from src.channels.config import load_channels_config
    from src.config import writer

    config_path = tmp_path / "agent.json"
    config_path.write_text(
        json.dumps({"channels": {"telegram": {"enabled": True}}}), encoding="utf-8"
    )

    writer.set_channel_section(
        "telegram",
        {"enabled": True, "token": "123:abc"},
        config_path=config_path,
    )

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["channels"]["telegram"] == {"enabled": True}

    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored == {"telegram": {"token": "123:abc"}}
    # The loader puts the credential back so adapters see one complete section.
    assert load_channels_config(config_path)["telegram"]["token"] == "123:abc"


def test_inline_credentials_migrate_on_first_write(tmp_path: Path) -> None:
    from src.config import writer

    config_path = tmp_path / "agent.json"
    config_path.write_text(
        json.dumps({"channels": {"weixin": {"enabled": True, "token": "legacy"}}}),
        encoding="utf-8",
    )

    writer.set_channel_section("weixin", {"enabled": True}, config_path=config_path)

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert "token" not in on_disk["channels"]["weixin"]
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored["weixin"]["token"] == "legacy", "an inline credential must never be lost"


def test_unbind_clears_the_secret_store(tmp_path: Path) -> None:
    from src.config import secrets, writer

    writer.set_channel_section(
        "slack", {"enabled": True, "bot_token": "xoxb-1"}, config_path=tmp_path / "agent.json"
    )
    assert secrets.clear_channel_secrets("slack", secrets_path=tmp_path / "secrets.json") is True
    assert not (tmp_path / "secrets.json").exists()


def test_writer_rejects_invalid_config_without_touching_the_file(tmp_path: Path) -> None:
    from src.config import writer

    config_path = tmp_path / "agent.json"
    original = json.dumps({"channels": {"telegram": {"enabled": True}}})
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(writer.ConfigWriteError):
        writer.set_channels_global({"auto_start": "definitely"}, config_path=config_path)

    assert json.loads(config_path.read_text(encoding="utf-8")) == json.loads(original)


def test_mask_secrets_hides_credentials() -> None:
    from src.config.writer import mask_secrets

    masked = mask_secrets({"enabled": True, "token": "abc", "allow_from": ["u"]})

    assert masked["token"] == ""
    assert masked["has_token"] is True
    assert masked["allow_from"] == ["u"]


class _FakeManager:
    """Minimal manager surface used by ChannelAuthService."""

    def __init__(self, adapter_cls: type) -> None:
        self._adapter_cls = adapter_cls
        self.restarted: list[str] = []

    def _resolve_class(self, name: str) -> type:
        assert name == "weixin"
        return self._adapter_cls

    def get_channel(self, name: str):
        return None

    async def restart_channel(self, name: str) -> bool:
        self.restarted.append(name)
        return True

    async def apply_section(self, name: str, section: Any) -> bool:
        return True

    async def start_channel(self, name: str) -> bool:
        return True


class _FakeQrAdapter:
    auth_kind = "qr"
    supports_interactive_login = True

    def __init__(self, section: Any, bus: Any) -> None:
        self.section = section
        self.bus = bus

    async def begin_login(self, *, on_qr=None, on_state=None) -> bool:
        if on_qr:
            on_qr("https://example.com/qr")
        if on_state:
            on_state("wait")
            on_state("scaned")
        await asyncio.sleep(0)
        return True


def test_auth_session_reaches_confirmed() -> None:
    from src.channels.auth import ChannelAuthService, STATE_CONFIRMED

    async def scenario() -> None:
        service = ChannelAuthService(
            manager=_FakeManager(_FakeQrAdapter),
            bus=MessageBus(),
            config_loader=lambda: {"weixin": {"enabled": False}},
            ttl_s=5,
        )
        session = await service.start_binding("weixin")
        await asyncio.sleep(0.05)
        assert session.qr_url == "https://example.com/qr"
        current = service.get("weixin", session.id)
        assert current is not None
        assert current.state == STATE_CONFIRMED

    asyncio.run(scenario())


def test_auth_session_rejects_channels_without_login_flow() -> None:
    from src.channels.auth import ChannelAuthService, LoginNotSupported

    class _NoLoginAdapter(_FakeQrAdapter):
        supports_interactive_login = False

    async def scenario() -> None:
        service = ChannelAuthService(
            manager=_FakeManager(_NoLoginAdapter),
            bus=MessageBus(),
            config_loader=lambda: {"weixin": {"enabled": True}},
        )
        with pytest.raises(LoginNotSupported):
            await service.start_binding("weixin")

    asyncio.run(scenario())


class _CrashChannel(_BlockingChannel):
    """Adapter that gives up right away, like a failed handshake."""

    async def start(self) -> None:
        self.started = True
        self._running = False


@pytest.fixture(autouse=True)
def _clean_log_buffers():
    """Keep module-level log buffers from leaking between tests."""
    from src.channels import logbuffer

    logbuffer.reset()
    yield
    logbuffer.reset()


def test_crashed_adapter_schedules_a_backed_off_retry() -> None:
    async def scenario() -> None:
        manager = ChannelManager(
            {"auto_start": True, "dummy": {"enabled": True}}, MessageBus()
        )
        channel = _CrashChannel()
        manager.channels["dummy"] = channel
        manager._desired["dummy"] = True

        await manager._run_channel("dummy", channel)

        status = manager.get_status()["dummy"]
        assert status["backoff_s"] == 1.0
        assert 0 < status["retry_in_s"] <= 1.0
        assert status["last_error"] == "adapter exited"

    asyncio.run(scenario())


def test_backoff_doubles_up_to_the_cap() -> None:
    manager = ChannelManager({"auto_start": True}, MessageBus())

    assert manager._next_delay("dummy") == 1.0
    manager._backoff["dummy"] = 1.0
    assert manager._next_delay("dummy") == 2.0
    manager._backoff["dummy"] = 16.0
    assert manager._next_delay("dummy") == 32.0
    manager._backoff["dummy"] = 60.0
    assert manager._next_delay("dummy") == 60.0


def test_supervisor_skips_channels_that_need_authorization() -> None:
    async def scenario() -> None:
        manager = ChannelManager(
            {"auto_start": True, "dummy": {"enabled": True}}, MessageBus()
        )
        channel = _CrashChannel()
        channel.needs_auth = True
        manager.channels["dummy"] = channel
        manager._desired["dummy"] = True

        respawned: list[str] = []

        async def fake_respawn(name: str) -> None:
            respawned.append(name)

        manager._respawn = fake_respawn  # type: ignore[method-assign]
        await manager._supervise_once()

        assert respawned == []
        assert manager.get_status()["dummy"]["state"] == STATE_NEEDS_AUTH

    asyncio.run(scenario())


def test_supervisor_restarts_a_channel_that_is_due() -> None:
    async def scenario() -> None:
        manager = ChannelManager(
            {"auto_start": True, "dummy": {"enabled": True}}, MessageBus()
        )
        manager.channels["dummy"] = _CrashChannel()
        manager._desired["dummy"] = True
        manager._next_retry_at["dummy"] = 0.0

        respawned: list[str] = []

        async def fake_respawn(name: str) -> None:
            respawned.append(name)

        manager._respawn = fake_respawn  # type: ignore[method-assign]
        await manager._supervise_once()

        assert respawned == ["dummy"]

    asyncio.run(scenario())


def test_supervisor_respects_a_deliberate_stop() -> None:
    async def scenario() -> None:
        manager = ChannelManager(
            {"auto_start": True, "dummy": {"enabled": True}}, MessageBus()
        )
        channel = _BlockingChannel()
        manager.channels["dummy"] = channel
        manager._status["dummy"] = {"enabled": True, "available": True}

        await manager.start_all()
        assert manager._desired["dummy"] is True

        await manager.stop_channel("dummy")

        assert manager._desired["dummy"] is False
        assert manager._next_retry_at.get("dummy") is None, "a stop must not schedule a retry"

    asyncio.run(scenario())


def test_channel_log_buffer_captures_adapter_output() -> None:
    manager = ChannelManager({"auto_start": True, "dummy": {"enabled": True}}, MessageBus())
    channel = _BlockingChannel()
    manager.channels["dummy"] = channel
    manager._attach_logs("dummy", channel)

    channel.logger.warning("boom from adapter")
    logging.getLogger("src.channels.dummy").error("module level failure")

    messages = [line["message"] for line in manager.channel_logs("dummy")]
    assert "boom from adapter" in messages
    assert "module level failure" in messages

    manager.clear_channel_logs("dummy")
    assert manager.channel_logs("dummy") == []
