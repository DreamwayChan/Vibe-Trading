"""IM channel HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_channels_routes(app, ...)``.

Covers four surfaces:

* lifecycle — start/stop/restart the runtime or a single adapter
* desired state — read/write the ``channels`` section of ``agent.json``
* binding — interactive login sessions (QR codes) and unbinding
* pairing — approve/deny DM senders that presented a pairing code
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models (defined locally -- NO shared modules, per maintainer rule)
# ---------------------------------------------------------------------------

class ChannelPairingCommandRequest(BaseModel):
    """Pairing command payload for IM channel sender pairing."""

    channel: str
    command: str


class ChannelAutoStartPatch(BaseModel):
    """Global autostart toggle written to ``channels.auto_start``."""

    auto_start: bool | None = None


class PairingApproval(BaseModel):
    """Approve or deny one pending pairing code."""

    code: str


# ---------------------------------------------------------------------------
# Lifecycle helpers (module-level, access host state via sys.modules)
# ---------------------------------------------------------------------------


def _host() -> Any:
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError("api_server module is not importable")
    return host


async def _start_channel_runtime(*, autostart_only: bool = False):
    """Start the IM channel runtime."""
    runtime = _host()._get_channel_runtime()
    await runtime.start(start_manager=True, autostart_only=autostart_only)
    return runtime


async def _stop_channel_runtime() -> None:
    """Stop the IM channel runtime if it was initialized."""
    host = _host()
    if host._channel_runtime is None:
        return
    await host._channel_runtime.stop()


def _runtime():
    """Late-access ``_get_channel_runtime`` for test monkeypatch compatibility."""
    return _host()._get_channel_runtime()


def channels_should_autostart() -> bool:
    """Resolve the effective autostart decision.

    Priority: explicit env override › ``channels.auto_start`` in the agent
    config (default true). Kept here (not in ``api_server``) so the server
    module stays a thin assembler.
    """
    from src.channels.config import load_channels_config
    from src.config.accessor import get_env_config

    env_override = get_env_config().agent_tuning.vibe_trading_channels_auto_start
    if env_override is not None:
        return bool(env_override)
    try:
        config = load_channels_config()
    except Exception:  # noqa: BLE001 - unreadable config falls back to default
        logger.warning("Could not load channels config; defaulting autostart to true")
        return True
    value = config.get("auto_start", config.get("autoStart")) if isinstance(config, dict) else None
    return value if isinstance(value, bool) else True


async def start_channels_in_background() -> None:
    """Start the channel runtime without letting failures reach the server."""
    try:
        await _start_channel_runtime(autostart_only=True)
    except Exception:  # noqa: BLE001 - startup background task must not crash
        logger.exception("IM channel autostart failed; continuing without channels")


def register_channels_startup(app: FastAPI) -> None:
    """Mount the channel autostart hook.

    Kept separate from the API server module so ``api_server.py`` stays a thin
    assembler, and so autostart can be exercised in isolation.
    """

    @app.on_event("startup")
    async def _channels_startup() -> None:
        if not channels_should_autostart():
            return
        try:
            asyncio.create_task(start_channels_in_background())
        except Exception:  # noqa: BLE001 - never block server startup
            logger.exception("Failed to schedule IM channel autostart")


# Auth sessions hold in-memory state, so the service must outlive a request.
_AUTH_SERVICE: Any = None
_AUTH_RUNTIME_ID: int | None = None


def _auth_service(runtime: Any):
    """Return the process-wide binding service for *runtime*."""
    global _AUTH_SERVICE, _AUTH_RUNTIME_ID
    if _AUTH_SERVICE is None or _AUTH_RUNTIME_ID != id(runtime):
        from src.channels.auth import ChannelAuthService

        _AUTH_SERVICE = ChannelAuthService(
            manager=runtime.manager,
            bus=runtime.bus,
        )
        _AUTH_RUNTIME_ID = id(runtime)
    return _AUTH_SERVICE


def _channel_entry(runtime: Any, name: str) -> dict[str, Any]:
    status = runtime.status()
    entry = status.get("channels", {}).get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {name}")
    return entry


def _config_writer():
    """Import the writer lazily so read-only deployments stay dependency-light."""
    from src.config import writer as config_writer

    return config_writer


def _current_section(name: str) -> dict[str, Any]:
    from src.channels.config import load_channels_config

    config = load_channels_config()
    section = config.get(name)
    return dict(section) if isinstance(section, dict) else {}


def _write_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_channels_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the channel routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    if require_auth is None:
        require_auth = _host().require_auth

    # --- Runtime + aggregate status ---

    @app.get("/channels/status", dependencies=[Depends(require_auth)])
    async def channels_status():
        """Return IM channel runtime and adapter status."""
        return _runtime().status()

    @app.post("/channels/start", dependencies=[Depends(require_auth)])
    async def channels_start():
        """Start every configured IM channel adapter."""
        runtime = await _start_channel_runtime()
        return {"status": "started", **runtime.status()}

    @app.post("/channels/stop", dependencies=[Depends(require_auth)])
    async def channels_stop():
        """Stop every running IM channel adapter."""
        runtime = _runtime()
        await runtime.stop()
        return {"status": "stopped", **runtime.status()}

    # --- Global settings (must precede /channels/{name}) ---

    @app.get("/channels/settings", dependencies=[Depends(require_auth)])
    async def channels_settings():
        """Return global channel settings and their environment override."""
        from src.channels.config import load_channels_config
        from src.config.accessor import get_env_config

        config = load_channels_config()
        value = config.get("auto_start", config.get("autoStart"))
        env_override = get_env_config().agent_tuning.vibe_trading_channels_auto_start
        return {
            "auto_start": value if isinstance(value, bool) else True,
            "env_override": env_override,
            "env_locked": env_override is not None,
        }

    @app.put("/channels/settings", dependencies=[Depends(require_auth)])
    async def update_channels_settings(payload: ChannelAutoStartPatch):
        """Persist the global autostart toggle."""
        if payload.auto_start is not None:
            writer = _config_writer()
            try:
                writer.set_channels_global({"auto_start": payload.auto_start})
            except Exception as exc:  # noqa: BLE001 - surfaced as 422
                raise _write_error(exc) from exc
            runtime = _runtime()
            if isinstance(runtime.manager.config, dict):
                runtime.manager.config["auto_start"] = payload.auto_start
        return await channels_settings()

    # --- Per-channel lifecycle ---

    @app.post("/channels/{name}/start", dependencies=[Depends(require_auth)])
    async def channel_start(name: str):
        """Start one channel adapter."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        started = await runtime.start_channel(name)
        if not started:
            raise HTTPException(
                status_code=409,
                detail=f"{name} could not be started; check /channels/status for the reason",
            )
        return {"status": "started", "channel": name, **runtime.status()}

    @app.post("/channels/{name}/stop", dependencies=[Depends(require_auth)])
    async def channel_stop(name: str):
        """Stop one channel adapter."""
        runtime = _runtime()
        await runtime.stop_channel(name)
        return {"status": "stopped", "channel": name, **runtime.status()}

    @app.post("/channels/{name}/restart", dependencies=[Depends(require_auth)])
    async def channel_restart(name: str):
        """Restart one channel adapter, rebuilding it from config."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        restarted = await runtime.restart_channel(name)
        if not restarted:
            raise HTTPException(status_code=409, detail=f"{name} could not be restarted")
        return {"status": "restarted", "channel": name, **runtime.status()}

    # --- Diagnostics ---

    @app.get("/channels/{name}/logs", dependencies=[Depends(require_auth)])
    async def channel_logs(name: str, limit: int = 100):
        """Return the adapter's most recent log lines.

        Adapters write to the process log, which is invisible when the server
        runs as a service; this is the only way the UI can explain a silent
        failure.
        """
        runtime = _runtime()
        _channel_entry(runtime, name)
        return {
            "channel": name,
            "lines": runtime.channel_logs(name, limit=max(1, min(int(limit), 500))),
        }

    @app.delete("/channels/{name}/logs", dependencies=[Depends(require_auth)])
    async def channel_logs_clear(name: str):
        """Drop the buffered log lines for one channel."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        runtime.clear_channel_logs(name)
        return {"status": "cleared", "channel": name}

    # --- Desired state (agent.json) ---

    @app.get("/channels/{name}", dependencies=[Depends(require_auth)])
    async def channel_detail(name: str):
        """Return one channel's masked config plus its live status."""
        runtime = _runtime()
        entry = _channel_entry(runtime, name)
        section = _current_section(name)
        return {
            "name": name,
            "config": _config_writer().mask_secrets(section),
            "configured": bool(section),
            "status": entry,
        }

    @app.patch("/channels/{name}", dependencies=[Depends(require_auth)])
    async def channel_patch(name: str, patch: dict[str, Any] = Body(...)):
        """Merge *patch* into one channel's config section and apply it live."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        section = _current_section(name)
        merged = {**section, **patch}
        if not merged:
            raise HTTPException(status_code=422, detail="refusing to write an empty channel section")
        writer = _config_writer()
        try:
            writer.set_channel_section(name, merged)
        except Exception as exc:  # noqa: BLE001 - surfaced as 422
            raise _write_error(exc) from exc
        await runtime.apply_section(name, merged)
        return {"status": "saved", "name": name, **runtime.status()}

    # --- Binding ---

    @app.post("/channels/{name}/auth/session", dependencies=[Depends(require_auth)])
    async def channel_auth_start(name: str):
        """Create a binding session (QR code for scan-based platforms)."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        from src.channels.auth import LoginNotSupported

        service = _auth_service(runtime)
        try:
            session = await service.start_binding(name)
        except LoginNotSupported as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} does not support scanning; provide its credentials via "
                    f"PATCH /channels/{name}"
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001 - report why the adapter refused
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return session.to_dict()

    @app.get("/channels/{name}/auth/session/{session_id}", dependencies=[Depends(require_auth)])
    async def channel_auth_poll(name: str, session_id: str):
        """Poll one binding session."""
        service = _auth_service(_runtime())
        session = service.get(name, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="binding session not found or expired")
        return session.to_dict()

    @app.delete("/channels/{name}/auth/session/{session_id}", dependencies=[Depends(require_auth)])
    async def channel_auth_cancel(name: str, session_id: str):
        """Cancel an in-flight binding session."""
        service = _auth_service(_runtime())
        if not service.cancel(name, session_id):
            raise HTTPException(status_code=404, detail="binding session not found")
        return {"status": "cancelled", "channel": name}

    @app.delete("/channels/{name}/auth", dependencies=[Depends(require_auth)])
    async def channel_unbind(name: str):
        """Forget the stored credential for one channel."""
        runtime = _runtime()
        _channel_entry(runtime, name)
        service = _auth_service(runtime)
        if not await service.logout(name):
            raise HTTPException(status_code=400, detail=f"{name} has no stored credential")
        return {"status": "unbound", "channel": name, **runtime.status()}

    # --- Pairing (DM sender approval) ---

    @app.get("/channels/pairing/pending", dependencies=[Depends(require_auth)])
    async def pairing_pending(channel: str | None = None):
        """List pending pairing requests, optionally scoped to one channel."""
        from src.channels.pairing import list_pending

        return {"pending": list_pending(restrict_channel=channel)}

    @app.get("/channels/pairing/approved", dependencies=[Depends(require_auth)])
    async def pairing_approved(channel: str | None = None):
        """List approved senders per channel."""
        from src.channels.pairing import get_approved

        if channel:
            return {"approved": {channel: get_approved(channel)}}
        from src.channels.config import load_channels_config

        names = {
            key
            for key, value in load_channels_config().items()
            if isinstance(value, dict)
        }
        return {"approved": {name: get_approved(name) for name in sorted(names)}}

    @app.post("/channels/pairing/approve", dependencies=[Depends(require_auth)])
    async def pairing_approve(payload: PairingApproval):
        """Approve a pending pairing code."""
        from src.channels.pairing import approve_code

        result = approve_code(payload.code)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown or expired pairing code")
        channel, sender_id = result
        return {"status": "approved", "channel": channel, "sender_id": sender_id}

    @app.post("/channels/pairing/deny", dependencies=[Depends(require_auth)])
    async def pairing_deny(payload: PairingApproval):
        """Deny a pending pairing code."""
        from src.channels.pairing import deny_code

        if not deny_code(payload.code):
            raise HTTPException(status_code=404, detail="unknown or expired pairing code")
        return {"status": "denied"}

    @app.delete("/channels/pairing/approved/{sender_id}", dependencies=[Depends(require_auth)])
    async def pairing_revoke(sender_id: str, channel: str):
        """Revoke a previously approved sender."""
        from src.channels.pairing import revoke

        if not revoke(channel, sender_id):
            raise HTTPException(status_code=404, detail="sender is not approved")
        return {"status": "revoked", "channel": channel, "sender_id": sender_id}

    # --- Backwards-compatible pairing command ---

    @app.post("/channels/pairing/command", dependencies=[Depends(require_auth)])
    async def channels_pairing_command(payload: ChannelPairingCommandRequest):
        """Run a pairing command against the shared pairing store."""
        from src.channels.pairing import handle_pairing_command

        return {
            "channel": payload.channel,
            "reply": handle_pairing_command(payload.channel, payload.command),
        }
