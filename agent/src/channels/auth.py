"""Interactive channel authentication (binding) sessions.

Adapters that need a human in the loop — QR-code logins for personal WeChat,
or credential entry for bot platforms — used to expose that flow only through
the CLI, which means the QR code was printed to whatever terminal happened to
own the process stdout.

This module turns a login attempt into a short-lived, addressable session:

    POST /channels/weixin/auth/session   -> {session_id, qr_svg, state}
    GET  /channels/weixin/auth/session/x -> {state: "scanned"}

The adapter drives the session from a background task; the browser polls it.
Sessions are bounded by a TTL, scoped to one channel, and never persist the
credential they obtain (that stays in the adapter's own state file).
"""

from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_WAITING_SCAN = "waiting_scan"
STATE_SCANNED = "scanned"
STATE_CONFIRMED = "confirmed"
STATE_EXPIRED = "expired"
STATE_FAILED = "failed"

_TERMINAL_STATES = frozenset({STATE_CONFIRMED, STATE_EXPIRED, STATE_FAILED})

DEFAULT_SESSION_TTL_S = 180.0

# Channels whose binding is a scan handshake. Kept as a static allowlist so the
# API can answer "this platform takes a token" without importing an adapter
# whose optional SDK may be missing.
INTERACTIVE_LOGIN_CHANNELS = frozenset({"weixin"})

# weixin protocol status -> session state
_WEIXIN_STATUS_MAP = {
    "wait": STATE_WAITING_SCAN,
    "scaned": STATE_SCANNED,
    "scaned_but_redirect": STATE_SCANNED,
    "confirmed": STATE_CONFIRMED,
    "expired": STATE_EXPIRED,
}


class LoginNotSupported(RuntimeError):
    """Raised when an adapter has no interactive login flow."""

    def __init__(self, channel: str) -> None:
        super().__init__(f"{channel} does not support interactive login")
        self.channel = channel


def render_qr_svg(url: str) -> str:
    """Render *url* as an inline SVG QR code.

    Returns an empty string when the optional ``qrcode`` dependency is missing;
    callers fall back to showing the raw login URL.
    """
    if not url:
        return ""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        image = qrcode.make(url, image_factory=SvgPathImage, border=2)
        buffer = io.BytesIO()
        image.save(buffer)
        return buffer.getvalue().decode("utf-8")
    except Exception:  # noqa: BLE001 - QR rendering is best-effort decoration
        logger.debug("QR SVG rendering unavailable", exc_info=True)
        return ""


@dataclass
class AuthSession:
    """One in-flight binding attempt for a single channel."""

    id: str
    channel: str
    kind: str = "qr"
    state: str = STATE_PENDING
    qr_url: str = ""
    qr_svg: str = ""
    detail: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    generation: int = 0

    @property
    def terminal(self) -> bool:
        """True once the session can no longer change state."""
        return self.state in _TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload for the REST layer."""
        return {
            "session_id": self.id,
            "channel": self.channel,
            "kind": self.kind,
            "state": self.state,
            "qr_url": self.qr_url,
            "qr_svg": self.qr_svg,
            "detail": self.detail,
            "error": self.error,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "expires_in": max(0, int(self.expires_at - time.time())),
        }

    # --- Mutators used from the adapter's background task ---

    def set_qr(self, url: str) -> None:
        """Attach a (possibly refreshed) QR payload to this session."""
        self.qr_url = url
        self.qr_svg = render_qr_svg(url)
        self.generation += 1
        if self.state in {STATE_PENDING, STATE_EXPIRED}:
            self.state = STATE_WAITING_SCAN

    def set_state(self, state: str, detail: str = "") -> None:
        """Advance the session state; terminal states are sticky."""
        if self.terminal:
            return
        self.state = state
        if detail:
            self.detail = detail

    def fail(self, error: str) -> None:
        """Mark the session failed with a human-readable reason."""
        if self.terminal:
            return
        self.state = STATE_FAILED
        self.error = error


class ChannelAuthService:
    """Owns binding sessions and the adapters that drive them.

    The service never touches a running adapter: it builds a throwaway instance
    from the current config section, lets it complete the platform handshake
    (which persists the credential to the adapter's state file), and then asks
    the manager to rebuild the live adapter from disk.
    """

    def __init__(
        self,
        *,
        manager: Any,
        bus: Any,
        config_loader: Callable[[], dict[str, Any]] | None = None,
        ttl_s: float = DEFAULT_SESSION_TTL_S,
    ) -> None:
        self._manager = manager
        self._bus = bus
        self._ttl_s = ttl_s
        self._sessions: dict[str, AuthSession] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._config_loader = config_loader

    # --- Config helpers ---------------------------------------------------

    def _load_config(self) -> dict[str, Any]:
        if self._config_loader is not None:
            return self._config_loader()
        from src.channels.config import load_channels_config

        return load_channels_config()

    def section(self, name: str) -> dict[str, Any]:
        """Return the current config section for *name* (empty dict if absent)."""
        config = self._load_config()
        section = config.get(name)
        return dict(section) if isinstance(section, dict) else {}

    def _build_adapter(self, name: str, cls: Any | None = None) -> Any:
        """Instantiate a throwaway adapter for the login handshake."""
        section = self.section(name)
        if not section:
            raise LoginNotSupported(name)
        adapter_cls = cls or self._manager._resolve_class(name)  # noqa: SLF001 - same package
        return adapter_cls(section, self._bus)

    # --- Session lifecycle ------------------------------------------------

    def create(self, name: str, *, kind: str = "qr") -> AuthSession:
        """Create and register a pending session for *name*."""
        self._gc()
        # One active session per channel: a stale QR must not linger next to a
        # fresh one in the UI.
        self._drop_channel_sessions(name)
        session = AuthSession(
            id=secrets.token_urlsafe(12),
            channel=name,
            kind=kind,
            state=STATE_WAITING_SCAN if kind == "qr" else STATE_PENDING,
            expires_at=time.time() + self._ttl_s,
        )
        self._sessions[session.id] = session
        return session

    def get(self, name: str, session_id: str) -> AuthSession | None:
        """Return a session if it belongs to *name* and has not expired."""
        session = self._sessions.get(session_id)
        if session is None or session.channel != name:
            return None
        if not session.terminal and time.time() > session.expires_at:
            session.set_state(STATE_EXPIRED, "二维码已过期，请刷新")
        return session

    def latest(self, name: str) -> AuthSession | None:
        """Return the most recent session for *name*, if any."""
        candidates = [s for s in self._sessions.values() if s.channel == name]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.created_at)

    def cancel(self, name: str, session_id: str) -> bool:
        """Abort an in-flight session."""
        session = self.get(name, session_id)
        if session is None:
            return False
        session.set_state(STATE_EXPIRED, "已取消")
        self._cancel_task(session.id)
        return True

    def _cancel_task(self, session_id: str) -> None:
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _drop_channel_sessions(self, name: str) -> None:
        for session_id, session in list(self._sessions.items()):
            if session.channel == name:
                self._cancel_task(session_id)
                self._sessions.pop(session_id, None)

    def _gc(self) -> None:
        """Drop sessions well past their TTL so the map cannot grow forever."""
        cutoff = time.time() - self._ttl_s
        for session_id, session in list(self._sessions.items()):
            if session.created_at < cutoff:
                self._cancel_task(session_id)
                self._sessions.pop(session_id, None)

    # --- Binding ----------------------------------------------------------

    async def start_binding(self, name: str) -> AuthSession:
        """Begin an interactive binding for *name*.

        Raises:
            LoginNotSupported: The adapter has no QR/interactive login; the UI
                must collect credentials instead.
        """
        # Decide from the class, not an instance: a missing optional SDK would
        # otherwise surface as "install the package" instead of "this platform
        # binds with a token".
        if name not in INTERACTIVE_LOGIN_CHANNELS:
            raise LoginNotSupported(name)
        cls = self._manager._resolve_class(name)  # noqa: SLF001 - same package
        if not getattr(cls, "supports_interactive_login", False):
            raise LoginNotSupported(name)

        adapter = self._build_adapter(name, cls)
        session = self.create(name, kind=getattr(cls, "auth_kind", "qr"))
        task = asyncio.create_task(self._run_login(session, adapter))
        self._tasks[session.id] = task
        task.add_done_callback(lambda _t, sid=session.id: self._tasks.pop(sid, None))
        return session

    async def _run_login(self, session: AuthSession, adapter: Any) -> None:
        """Drive one adapter login to completion inside the session TTL."""
        try:
            ok = await asyncio.wait_for(
                adapter.begin_login(
                    on_qr=session.set_qr,
                    on_state=lambda status: self._on_adapter_state(session, status),
                ),
                timeout=self._ttl_s + 5,
            )
        except asyncio.TimeoutError:
            session.set_state(STATE_EXPIRED, "二维码已过期，请刷新")
            return
        except asyncio.CancelledError:
            raise
        except LoginNotSupported:
            session.fail("该平台不支持扫码，请填写凭据")
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the binding wizard
            logger.exception("%s login failed", session.channel)
            session.fail(f"{type(exc).__name__}: {exc}")
            return

        if not ok:
            session.fail(session.detail or "登录未完成")
            return

        session.state = STATE_CONFIRMED
        session.detail = "绑定成功"
        await self._after_bind(session)

    @staticmethod
    def _on_adapter_state(session: AuthSession, status: str) -> None:
        mapped = _WEIXIN_STATUS_MAP.get(status)
        if mapped is None:
            return
        if mapped == STATE_EXPIRED:
            # the adapter refreshes the QR itself; keep the session usable
            session.detail = "二维码已刷新"
            return
        session.set_state(mapped)

    async def _after_bind(self, session: AuthSession) -> None:
        """Restart the live adapter so it picks up the new credential."""
        name = session.channel
        section = self.section(name)
        if not section.get("enabled"):
            return
        try:
            if self._manager.get_channel(name) is not None:
                await self._manager.restart_channel(name)
            else:
                await self._manager.apply_section(name, section)
                await self._manager.start_channel(name)
        except Exception:  # noqa: BLE001 - binding already succeeded
            logger.exception("Bound %s but could not start it", name)

    async def logout(self, name: str) -> bool:
        """Drop the stored credential for *name* and stop using it.

        Covers both credential styles: values kept in ``secrets.json`` (bot
        tokens, app secrets) and credentials an adapter persists itself
        (WeChat's account file).
        """
        from src.config.secrets import clear_channel_secrets

        cleared = clear_channel_secrets(name)
        channel = self._manager.get_channel(name)
        adapter = channel
        if adapter is None:
            try:
                adapter = self._build_adapter(name)
            except Exception:  # noqa: BLE001 - missing SDK must not block unbinding
                adapter = None
        if adapter is not None:
            await adapter.logout()
        if channel is not None:
            channel.mark_needs_auth("logged out")
            if channel.is_running:
                # Keep running with a revoked credential only produces errors.
                await self._manager.stop_channel(name)
        return cleared or adapter is not None
