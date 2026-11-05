"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from src.channels.base import BaseChannel
from src.channels.bus.events import OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.logbuffer import attach_logger, get_buffer
from src.channels.registry import (
    discover_channel_names,
    discover_plugins,
    inspect_channels,
    load_channel_class,
)
from src.config.paths import get_workspace_path

logger = logging.getLogger(__name__)

# Retry delays for message sending (exponential backoff: 1s, 2s, 4s)
_SEND_RETRY_DELAYS = (1, 2, 4)

# How long start_all() waits for adapters to report early failures. Long-poll
# adapters (WeChat, Telegram) only return when stopped, so this must never be
# an unbounded gather.
_START_PROBE_TIMEOUT_S = 1.5

# Upper bound for a graceful adapter shutdown before the task is cancelled.
_STOP_TIMEOUT_S = 5.0

# Supervisor: an adapter that exits without being asked to is restarted with
# exponential backoff so a flapping upstream cannot spin the process.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0
# Stay up this long and the next crash starts the backoff over at 1s.
_BACKOFF_RESET_AFTER_S = 60.0
_SUPERVISOR_INTERVAL_S = 1.0

# Exception text that means "the platform rejected our credential" rather than
# "the network hiccuped". Matching here keeps the UI honest: a 401 must offer a
# re-bind, not an endless restart loop.
_AUTH_ERROR_MARKERS = (
    "unauthorized",
    "invalid_auth",
    "invalid token",
    "invalid_token",
    "authentication failed",
    "401",
    "403",
    "errcode -14",
)

# Channel lifecycle states surfaced to the UI. Keep these strings stable: the
# frontend switches on them.
STATE_DISABLED = "disabled"
STATE_UNAVAILABLE = "unavailable"
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_NEEDS_AUTH = "needs_auth"
STATE_ERROR = "error"

def _looks_like_auth_error(exc: BaseException) -> bool:
    """Return True when *exc* means the platform rejected our credential.

    Used to flip a channel into ``needs_auth`` instead of letting the supervisor
    retry a request that can never succeed until someone re-binds.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _AUTH_ERROR_MARKERS)


_BOOL_CAMEL_ALIASES: dict[str, str] = {
    "send_progress": "sendProgress",
    "send_tool_hints": "sendToolHints",
    "show_reasoning": "showReasoning",
    "auto_start": "autoStart",
}


class ChannelManager:
    """Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, Discord, Slack, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    def __init__(
        self,
        config: Any,  # ChannelsConfig or dict
        bus: MessageBus,
        *,
        session_service: Any | None = None,
        cron_service: Any | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self._session_service = session_service
        self._cron_service = cron_service
        self.channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._origin_reply_fingerprints: dict[tuple[str, str, str], str] = {}
        self._status: dict[str, dict[str, Any]] = {}
        # Per-channel lifecycle tracking. ``_tasks`` holds the long-running
        # adapter task so one stuck adapter can never block the others.
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_error: dict[str, str] = {}
        self._restarts: dict[str, int] = {}
        self._started_at: dict[str, float] = {}
        # Supervisor state: ``_desired`` separates "the operator wants this
        # channel up" from "the adapter is currently up", which is what lets a
        # crash be distinguished from a deliberate stop.
        self._desired: dict[str, bool] = {}
        self._backoff: dict[str, float] = {}
        self._next_retry_at: dict[str, float] = {}
        self._supervisor_task: asyncio.Task[None] | None = None
        self._class_cache: dict[str, type[BaseChannel]] = {}
        self._builtin_names: set[str] = set()

        self._init_channels()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        self._status = inspect_channels(self.config)
        enabled_names = {
            name
            for name, status in self._status.items()
            if status.get("enabled") is True
        }

        for name in sorted(enabled_names):
            self._create_channel(name)

        self._validate_allow_from()

    def _create_channel(self, name: str) -> BaseChannel | None:
        """Build and register one channel adapter from the current config.

        Returns the channel instance, or ``None`` when the adapter is missing a
        dependency or failed to construct. Failures are recorded in ``_status``
        so ``/channels/status`` can explain the gap instead of raising.
        """
        section = self._get_channel_config(name)
        if section is None:
            return None

        self._status.setdefault(name, {})
        # Bind the buffer even when the adapter fails to load: the reason is
        # exactly what the operator needs to read in the UI.
        self._attach_logs(name)
        if self._status[name].get("available") is False:
            logger.warning(
                "%s channel not available: %s",
                name,
                self._status[name].get("error", "unavailable"),
            )
            return None

        try:
            cls = self._resolve_class(name)
        except Exception as exc:  # noqa: BLE001 - status endpoint must explain startup gaps
            self._status[name].update(
                {
                    "available": False,
                    "loaded": False,
                    "running": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.warning("%s channel not available: %s", name, exc)
            return None

        try:
            kwargs = self._build_channel_kwargs(name)
            channel = cls(section, self.bus, **kwargs)
            # Resolve global → per-channel boolean overrides
            channel.send_progress = self._resolve_bool_override(
                section, "send_progress", self._global_bool("send_progress", True),
            )
            channel.send_tool_hints = self._resolve_bool_override(
                section, "send_tool_hints", self._global_bool("send_tool_hints", False),
            )
            channel.show_reasoning = self._resolve_bool_override(
                section, "show_reasoning", self._global_bool("show_reasoning", True),
            )
            self.channels[name] = channel
            self._status[name].update(
                {
                    "available": True,
                    "loaded": True,
                    "running": channel.is_running,
                    "display_name": getattr(cls, "display_name", name),
                    "error": "",
                }
            )
            self._last_error.pop(name, None)
            self._attach_logs(name, channel)
            logger.info("%s channel enabled", cls.display_name)
            return channel
        except Exception as exc:
            self._status[name].update(
                {
                    "loaded": False,
                    "running": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self._last_error[name] = f"{type(exc).__name__}: {exc}"
            logger.warning("%s channel not available", name, exc_info=True)
            return None

    # --- Supervision & observability ---

    def _attach_logs(self, name: str, channel: BaseChannel | None = None) -> None:
        """Bind a bounded log buffer to this channel's loggers.

        Adapters log through two names: the module logger
        (``src.channels.<name>``) and the per-instance logger created by
        :class:`BaseChannel`. Both feed the same buffer so the UI sees one
        stream.
        """
        get_buffer(name)
        logger_names = {f"src.channels.{name}"}
        if channel is not None:
            logger_names.add(channel.logger.name)
        for logger_name in logger_names:
            attach_logger(logger_name, channel=name)

    def _note(self, name: str, level: int, message: str) -> None:
        """Log a lifecycle event and keep it in the channel's buffer."""
        get_buffer(name).append(logging.getLevelName(level), message, source="manager")
        logger.log(level, message)

    def channel_logs(self, name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent buffered log records for one channel."""
        return get_buffer(name).snapshot(limit=limit)

    def clear_channel_logs(self, name: str) -> None:
        """Empty one channel's log buffer."""
        get_buffer(name).clear()

    def _next_delay(self, name: str) -> float:
        """Backoff delay to apply *after* another crash of *name*."""
        previous = self._backoff.get(name, 0.0)
        if previous <= 0:
            return _BACKOFF_INITIAL_S
        return min(previous * 2, _BACKOFF_MAX_S)

    def _schedule_retry(self, name: str, delay: float | None = None) -> float:
        """Record the next restart time for *name* and return the delay used."""
        wait = self._next_delay(name) if delay is None else delay
        self._backoff[name] = wait
        self._next_retry_at[name] = time.time() + wait
        return wait

    def _reset_backoff(self, name: str) -> None:
        """Forget the backoff after a channel stayed up long enough."""
        self._backoff.pop(name, None)
        self._next_retry_at.pop(name, None)

    def _auto_restart_enabled(self, name: str) -> bool:
        """Whether the supervisor may restart *name* after an unexpected exit."""
        if not self._global_bool("supervisor", True):
            return False
        section = self._get_channel_config(name)
        if section is not None:
            value = self._section_value(section, "auto_restart")
            if isinstance(value, bool):
                return value
        return True

    def _ensure_supervisor(self) -> None:
        """Start the watchdog task if it is not already running."""
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        try:
            self._supervisor_task = asyncio.create_task(self._supervise_loop())
        except RuntimeError:
            # No running loop (e.g. manager constructed in a sync context).
            self._supervisor_task = None

    async def _supervise_loop(self) -> None:
        """Periodically restart adapters that exited without being stopped."""
        while True:
            try:
                await asyncio.sleep(_SUPERVISOR_INTERVAL_S)
                await self._supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watchdog must never die
                logger.exception("Channel supervisor iteration failed")

    async def _supervise_once(self) -> None:
        """One watchdog pass: reset healthy backoffs, restart due channels."""
        now = time.time()
        for name in list(self.channels):
            channel = self.channels[name]
            task = self._tasks.get(name)
            if task is not None and not task.done():
                started = self._started_at.get(name)
                if started is not None and now - started >= _BACKOFF_RESET_AFTER_S:
                    self._reset_backoff(name)
                continue
            if not self._desired.get(name) or not self._auto_restart_enabled(name):
                continue
            if channel.is_running:
                # Adapter started its own background task and let start()
                # return: it is healthy, so nothing to supervise.
                continue
            if channel.needs_auth:
                # A dead credential cannot be fixed by retrying; wait for the
                # operator to re-bind (or for the adapter's own pause to elapse).
                continue
            if now < self._next_retry_at.get(name, 0.0):
                continue
            await self._respawn(name)

    async def _respawn(self, name: str) -> None:
        """Rebuild and restart one crashed adapter."""
        channel = self.channels.get(name)
        if channel is None:
            return
        # Rebuild from config: a crashed adapter can hold half-closed clients
        # and a stale credential snapshot.
        self.channels.pop(name, None)
        fresh = self._create_channel(name)
        if fresh is None:
            # A missing SDK or an invalid section cannot be fixed by retrying,
            # so stop here and let the operator act on the reported error.
            self._desired[name] = False
            self._note(
                name,
                logging.ERROR,
                "adapter could not be rebuilt; automatic restart stopped",
            )
            return
        self._restarts[name] = self._restarts.get(name, 0) + 1
        wait = self._schedule_retry(name)
        self._note(
            name,
            logging.WARNING,
            f"adapter exited unexpectedly; restart #{self._restarts[name]} "
            f"(next retry in {wait:.0f}s if this attempt fails)",
        )
        await self.start_channel(name, supervise=False)

    def _resolve_class(self, name: str) -> type[BaseChannel]:
        """Resolve a channel name to its adapter class (cached after first hit)."""
        cached = self._class_cache.get(name)
        if cached is not None:
            return cached
        if not self._builtin_names:
            self._builtin_names = set(discover_channel_names())
        if name in self._builtin_names:
            cls = load_channel_class(name)
        else:
            plugins = discover_plugins({name})
            if name not in plugins:
                raise KeyError(f"channel '{name}' is not a built-in adapter or a registered plugin")
            cls = plugins[name]
        self._class_cache[name] = cls
        return cls

    def _section_value(self, section: Any, key: str) -> Any:
        """Read *key* (or its camelCase alias) from a config section."""
        if isinstance(section, dict):
            if key in section:
                return section[key]
            camel = _BOOL_CAMEL_ALIASES.get(key)
            return section.get(camel) if camel else None
        return getattr(section, key, None)

    def auto_start_enabled(self, name: str) -> bool:
        """Return whether *name* should be brought up with the runtime.

        Resolution order: per-channel ``auto_start`` → global ``auto_start``
        (default ``True``). Only meaningful for enabled channels.
        """
        section = self._get_channel_config(name)
        if section is None:
            return False
        if not self._status.get(name, {}).get("enabled", False):
            return False
        value = self._section_value(section, "auto_start")
        if isinstance(value, bool):
            return value
        return bool(self._global_bool("auto_start", True))

    def _build_channel_kwargs(self, name: str) -> dict[str, Any]:
        """Build adapter-specific constructor kwargs for channels that need services."""
        if name == "websocket":
            from src.channelsui.gateway_services import build_gateway_services

            return {
                "gateway": build_gateway_services(
                    session_manager=self._session_service,
                    cron_service=self._cron_service,
                    workspace_path=get_workspace_path(),
                )
            }
        if name == "matrix":
            return {
                "restrict_to_workspace": self._global_bool("restrict_to_workspace", False),
                "workspace": get_workspace_path(),
            }
        return {}

    def _get_channel_config(self, name: str) -> dict | None:
        """Get the config section for channel *name*."""
        if isinstance(self.config, dict):
            return self.config.get(name)
        return getattr(self.config, name, None)

    def _global_bool(self, key: str, default: bool) -> bool:
        """Read a global boolean from the top-level channels config."""
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def _validate_allow_from(self) -> None:
        for name, ch in self.channels.items():
            cfg = ch.config
            if isinstance(cfg, dict):
                allow = cfg.get("allow_from") if "allow_from" in cfg else cfg.get("allowFrom")
            else:
                allow = getattr(cfg, "allow_from", None)
            if allow is None:
                logger.info(
                    '"%s" has no allowFrom; unapproved users will receive a pairing code',
                    name,
                )

    def _should_send_progress(self, channel_name: str, *, tool_hint: bool = False) -> bool:
        """Return whether progress (or tool-hints) may be sent to *channel_name*."""
        ch = self.channels.get(channel_name)
        if ch is None:
            logger.debug("Progress check for unknown channel: %s", channel_name)
            return False
        return ch.send_tool_hints if tool_hint else ch.send_progress

    def _resolve_bool_override(self, section: Any, key: str, default: bool) -> bool:
        """Return *key* from *section* if it is a bool, otherwise *default*.

        For dict configs also checks the camelCase alias.
        """
        if isinstance(section, dict):
            value = section.get(key)
            if value is None:
                camel = _BOOL_CAMEL_ALIASES.get(key)
                if camel:
                    value = section.get(camel)
            return value if isinstance(value, bool) else default
        value = getattr(section, key, None)
        return value if isinstance(value, bool) else default

    async def _run_channel(self, name: str, channel: BaseChannel) -> None:
        """Own one adapter's long-running ``start()`` and record the outcome.

        Long-poll adapters never return from ``start()`` until stopped, so this
        coroutine is always run as a task and never awaited inline. When it
        returns without the operator asking for a stop, the supervisor takes
        over and schedules a restart.
        """
        cancelled = False
        try:
            await channel.start()
            if channel.needs_auth:
                self._last_error[name] = self._last_error.get(name, "authentication required")
            elif not channel.is_running:
                self._last_error.setdefault(name, "adapter exited")
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced via /channels/status
            self._last_error[name] = f"{type(exc).__name__}: {exc}"
            if _looks_like_auth_error(exc):
                channel.mark_needs_auth(str(exc))
            logger.exception("Failed to start channel %s", name)
        finally:
            self._refresh_status()
            if not cancelled and self._desired.get(name):
                if channel.needs_auth:
                    self._note(
                        name,
                        logging.WARNING,
                        f"adapter stopped and needs authentication: {self._last_error.get(name, '')}".strip(),
                    )
                elif channel.is_running:
                    # Some adapters return from start() while keeping their own
                    # background task; they are healthy, not crashed.
                    self._last_error.pop(name, None)
                elif self._auto_restart_enabled(name):
                    wait = self._schedule_retry(name)
                    self._note(
                        name,
                        logging.WARNING,
                        f"adapter stopped unexpectedly; restarting in {wait:.0f}s",
                    )

    def _refresh_status(self) -> None:
        """Sync the cached status dict with live adapter state."""
        for name, channel in self.channels.items():
            self._status.setdefault(name, {})
            self._status[name]["running"] = channel.is_running
            self._status[name]["needs_auth"] = channel.needs_auth
            if channel.is_running:
                self._started_at.setdefault(name, time.time())
            else:
                self._started_at.pop(name, None)

    async def start_all(self, *, autostart_only: bool = False) -> None:
        """Start enabled channels without blocking on long-poll adapters.

        Args:
            autostart_only: When True, skip channels whose ``auto_start`` is
                disabled. Used on process startup; an explicit ``/channels/start``
                call starts every enabled adapter.
        """
        if not self.channels:
            logger.warning("No channels enabled")
            return

        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        tasks: list[asyncio.Task[None]] = []
        for name, channel in self.channels.items():
            if autostart_only and not self.auto_start_enabled(name):
                logger.info("Skipping %s channel (auto_start disabled)", name)
                continue
            existing = self._tasks.get(name)
            if existing is not None and not existing.done():
                self._desired[name] = True
                continue
            logger.info("Starting %s channel...", name)
            self._desired[name] = True
            task = asyncio.create_task(self._run_channel(name, channel))
            self._tasks[name] = task
            self._started_at[name] = time.time()
            tasks.append(task)

        self._ensure_supervisor()
        if tasks:
            # Bounded probe: surfaces adapters that fail immediately, without
            # waiting for long-poll adapters that only return on stop.
            await asyncio.wait(tasks, timeout=_START_PROBE_TIMEOUT_S)
        self._refresh_status()

    async def start_channel(self, name: str, *, supervise: bool = True) -> bool:
        """Start a single enabled channel. Returns False if it is not loadable.

        Args:
            name: Channel name.
            supervise: When True (the default), the watchdog may restart the
                adapter if it exits later. ``False`` is used by the supervisor
                itself so a failing restart does not re-enter.
        """
        section = self._get_channel_config(name)
        if section is None or self._section_value(section, "enabled") is not True:
            logger.info("Refusing to start %s: channel is not enabled in config", name)
            return False
        channel = self.channels.get(name)
        if channel is None:
            # Channel exists in config but was never constructed (disabled at
            # boot). Build it now so "enable → start" works without a restart.
            channel = self._create_channel(name)
            if channel is None:
                return False
        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            self._desired[name] = True
            return True
        self._desired[name] = True
        if supervise:
            self._ensure_supervisor()
        task = asyncio.create_task(self._run_channel(name, channel))
        self._tasks[name] = task
        self._started_at[name] = time.time()
        await asyncio.wait({task}, timeout=_START_PROBE_TIMEOUT_S)
        self._refresh_status()
        return (not task.done()) or channel.is_running

    async def stop_channel(self, name: str) -> bool:
        """Stop one channel and its adapter task."""
        channel = self.channels.get(name)
        task = self._tasks.pop(name, None)
        if channel is None and task is None:
            return False
        # Marking it not-desired first is what tells the supervisor (and the
        # adapter task's finally block) that this exit was intentional.
        self._desired[name] = False
        self._reset_backoff(name)
        if channel is not None:
            try:
                # Give the adapter a chance to close connections cleanly, then
                # cancel whatever is still pending (long-poll loops ignore stop
                # signals until their current request returns).
                await asyncio.wait_for(channel.stop(), timeout=_STOP_TIMEOUT_S)
                logger.info("Stopped %s channel", name)
            except asyncio.TimeoutError:
                logger.warning("%s channel stop timed out; cancelling task", name)
            except asyncio.CancelledError:
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise
                logger.debug("Channel %s stop task was already cancelled", name)
            except Exception:
                logger.exception("Error stopping %s", name)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._started_at.pop(name, None)
        self._refresh_status()
        return True

    async def restart_channel(self, name: str) -> bool:
        """Stop then start one channel, counting the restart."""
        if name not in self.channels and self._get_channel_config(name) is None:
            return False
        await self.stop_channel(name)
        self._restarts[name] = self._restarts.get(name, 0) + 1
        # Rebuild from the current config so credential changes take effect.
        self.channels.pop(name, None)
        self._create_channel(name)
        return await self.start_channel(name)

    async def apply_section(self, name: str, section: Any) -> bool:
        """Apply an updated config section to one channel at runtime.

        Stops the adapter, rebuilds it from *section*, and restarts it if it
        was running before.
        """
        was_running = bool(self.channels.get(name) and self.channels[name].is_running)
        was_desired = self._desired.get(name, False)
        if self.channels.get(name) is not None or name in self._tasks:
            await self.stop_channel(name)
        self.channels.pop(name, None)
        if isinstance(self.config, dict):
            self.config[name] = section
        self._status.setdefault(name, {}).update(
            {
                "configured": section is not None,
                "enabled": self._section_value(section, "enabled") is True
                if section is not None
                else False,
            }
        )
        if section is None:
            return True
        channel = self._create_channel(name) if self._status.get(name, {}).get("enabled") else None
        if channel is not None and (was_running or was_desired):
            return await self.start_channel(name)
        return True

    async def stop_all(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None

        if self._dispatch_task:
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None

        for name in list(self._desired):
            self._desired[name] = False
        for name in list(self.channels):
            await self.stop_channel(name)

        self._started_at.clear()
        self._backoff.clear()
        self._next_retry_at.clear()

    # --- Outbound dispatch ---

    @staticmethod
    def _fingerprint_content(content: str) -> str:
        normalized = " ".join(content.split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest() if normalized else ""

    def _should_suppress_outbound(self, msg: OutboundMessage) -> bool:
        metadata = msg.metadata or {}
        if metadata.get("_progress"):
            return False
        fingerprint = self._fingerprint_content(msg.content)
        if not fingerprint:
            return False

        origin_message_id = metadata.get("origin_message_id")
        if isinstance(origin_message_id, str) and origin_message_id:
            key = (msg.channel, msg.chat_id, origin_message_id)
            if self._origin_reply_fingerprints.get(key) == fingerprint:
                return True
            self._origin_reply_fingerprints[key] = fingerprint

        message_id = metadata.get("message_id")
        if isinstance(message_id, str) and message_id:
            key = (msg.channel, msg.chat_id, message_id)
            self._origin_reply_fingerprints[key] = fingerprint

        return False

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        pending: list[OutboundMessage] = []

        while True:
            try:
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(
                        self.bus.consume_outbound(), timeout=1.0,
                    )

                # Reasoning routing
                if (
                    msg.metadata.get("_reasoning_delta")
                    or msg.metadata.get("_reasoning_end")
                    or msg.metadata.get("_reasoning")
                ):
                    channel = self.channels.get(msg.channel)
                    if channel is not None and channel.show_reasoning:
                        await self._send_with_retry(channel, msg)
                    continue

                # Progress / tool-hint filtering
                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=True,
                    ):
                        continue
                    if not msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=False,
                    ):
                        continue

                if msg.metadata.get("_retry_wait"):
                    continue

                # Coalesce consecutive _stream_delta messages
                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    if (
                        not msg.metadata.get("_stream_delta")
                        and not msg.metadata.get("_stream_end")
                        and not msg.metadata.get("_streamed")
                    ):
                        if self._should_suppress_outbound(msg):
                            logger.info(
                                "Suppressing duplicate outbound message to %s:%s",
                                msg.channel, msg.chat_id,
                            )
                            continue
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: %s", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """Send one outbound message without retry policy."""
        if msg.metadata.get("_reasoning_end"):
            await channel.send_reasoning_end(msg.chat_id, msg.metadata)
        elif msg.metadata.get("_reasoning_delta"):
            await channel.send_reasoning_delta(msg.chat_id, msg.content, msg.metadata)
        elif msg.metadata.get("_reasoning"):
            await channel.send_reasoning(msg)
        elif msg.metadata.get("_file_edit_events"):
            edits = msg.metadata.get("_file_edit_events")
            await channel.send_file_edit_events(
                msg.chat_id,
                edits if isinstance(edits, list) else [],
                msg.metadata,
            )
        elif msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage,
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """Merge consecutive _stream_delta messages for the same target.

        This reduces the number of API calls when the queue has accumulated
        multiple deltas.
        """
        first_metadata = first_msg.metadata or {}
        target_key = (
            first_msg.channel,
            first_msg.chat_id,
            first_metadata.get("_stream_id"),
        )
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            next_metadata = next_msg.metadata or {}
            same_target = (
                next_msg.channel,
                next_msg.chat_id,
                next_metadata.get("_stream_id"),
            ) == target_key
            is_delta = next_metadata.get("_stream_delta")
            is_end = next_metadata.get("_stream_end")

            if same_target and is_delta and not final_metadata.get("_stream_end"):
                combined_content += next_msg.content
                if is_end:
                    final_metadata["_stream_end"] = True
                    break
            else:
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(
        self, channel: BaseChannel, msg: OutboundMessage,
    ) -> None:
        """Send a message with retry on failure using exponential backoff."""
        max_attempts = 2  # conservative default
        if isinstance(self.config, dict):
            max_attempts = max(self.config.get("send_max_retries", 2), 1)
        elif hasattr(self.config, "send_max_retries"):
            max_attempts = max(self.config.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to send to %s after %d attempts",
                        msg.channel, max_attempts,
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to %s failed (attempt %d/%d): %s, retrying in %ds",
                    msg.channel, attempt + 1, max_attempts, type(e).__name__, delay,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise

    # --- Public API ---

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self.channels.get(name)

    def state_of(self, name: str) -> str:
        """Derive the lifecycle state for *name* from live adapter state."""
        entry = self._status.get(name, {})
        channel = self.channels.get(name)
        # Live state wins: an adapter that is actually running is running even
        # if the cached config snapshot disagrees.
        if channel is not None:
            if channel.needs_auth:
                return STATE_NEEDS_AUTH
            if channel.is_running:
                return STATE_RUNNING
            task = self._tasks.get(name)
            if task is not None and not task.done():
                return STATE_STARTING
        if entry.get("enabled") is not True:
            return STATE_DISABLED
        if entry.get("available") is False:
            return STATE_UNAVAILABLE
        if channel is not None:
            return STATE_STOPPED
        if entry.get("loaded") is False:
            return STATE_ERROR
        return STATE_STOPPED

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        status = {name: dict(item) for name, item in self._status.items()}
        for name, channel in self.channels.items():
            status.setdefault(name, {})
            status[name].update(
                {
                    "enabled": True,
                    "loaded": True,
                    "running": channel.is_running,
                    "display_name": getattr(channel, "display_name", name),
                }
            )
        for name, entry in status.items():
            entry["state"] = self.state_of(name)
            entry["auto_start"] = self.auto_start_enabled(name)
            entry["auto_restart"] = self._auto_restart_enabled(name)
            entry["desired"] = bool(self._desired.get(name))
            entry["restarts"] = self._restarts.get(name, 0)
            error = self._last_error.get(name) or entry.get("error") or ""
            entry["last_error"] = error
            backoff = self._backoff.get(name)
            entry["backoff_s"] = round(backoff, 1) if backoff else 0.0
            retry_at = self._next_retry_at.get(name)
            entry["retry_in_s"] = round(max(0.0, retry_at - time.time()), 1) if retry_at else 0.0
            started_at = self._started_at.get(name)
            entry["uptime_s"] = (
                round(time.time() - started_at, 1) if started_at else 0.0
            )
        return status

    @property
    def running_channels(self) -> list[str]:
        """Names of adapters that are currently running."""
        return [name for name, ch in self.channels.items() if ch.is_running]

    @property
    def supervisor_running(self) -> bool:
        """Whether the restart watchdog is active."""
        return self._supervisor_task is not None and not self._supervisor_task.done()

    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self.channels.keys())
