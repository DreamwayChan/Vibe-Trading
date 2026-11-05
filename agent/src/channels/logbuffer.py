"""Per-channel in-memory log buffer.

IM adapters log to stderr, which is invisible when the server runs as a
background service or inside a container. The Settings panel needs to explain
*why* a channel is not connecting, so each adapter keeps a bounded ring buffer
of its most recent records that the UI can read over HTTP.

Design constraints:

* **Bounded** — a misbehaving adapter must not grow memory without limit; the
  buffer drops the oldest record once ``capacity`` is reached.
* **Passive** — installing a handler never changes the level of the channel
  logger, so it cannot accidentally make the service noisier.
* **Cheap** — formatting happens in :meth:`ChannelLogBuffer.add_record` on the
  logging thread, which is the same thread the record was produced on, so a
  slow reader never blocks the adapter.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

# One buffer holds roughly the last few minutes of a chatty adapter.
DEFAULT_CAPACITY = 200
# Trim individual records so a stack trace cannot dominate the payload.
MAX_MESSAGE_CHARS = 4000

_BUFFERS: dict[str, ChannelLogBuffer] = {}
_HANDLERS: dict[str, _BufferHandler] = {}
_LOCK = threading.Lock()


class ChannelLogBuffer:
    """Bounded, thread-safe ring buffer of formatted log records."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._records: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    def append(self, level: str, message: str, *, source: str = "") -> None:
        """Record one already-formatted line."""
        text = (message or "").strip()
        if not text:
            return
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "…"
        with self._lock:
            self._records.append(
                {
                    "ts": time.time(),
                    "level": level.lower(),
                    "source": source,
                    "message": text,
                }
            )

    def add_record(self, record: logging.LogRecord) -> None:
        """Record a :class:`logging.LogRecord` including its traceback."""
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not raise
            message = str(record.msg)
        if record.exc_info:
            try:
                message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            except Exception:  # noqa: BLE001 - keep the plain message
                pass
        self.append(record.levelname, message, source=record.name)

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the newest *limit* records, oldest first."""
        size = max(1, min(int(limit), self.capacity))
        with self._lock:
            items = list(self._records)[-size:]
        return [dict(item) for item in items]

    def clear(self) -> None:
        """Drop every buffered record."""
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class _BufferHandler(logging.Handler):
    """Logging handler that forwards records into a :class:`ChannelLogBuffer`."""

    def __init__(self, buffer: ChannelLogBuffer) -> None:
        super().__init__(level=logging.NOTSET)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.add_record(record)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


def get_buffer(name: str, capacity: int = DEFAULT_CAPACITY) -> ChannelLogBuffer:
    """Return (creating if needed) the buffer registered for *name*."""
    with _LOCK:
        buffer = _BUFFERS.get(name)
        if buffer is None:
            buffer = ChannelLogBuffer(capacity=capacity)
            _BUFFERS[name] = buffer
        return buffer


def attach_logger(logger_name: str, *, channel: str, capacity: int = DEFAULT_CAPACITY) -> ChannelLogBuffer:
    """Buffer everything logged by *logger_name* under the *channel* key.

    Safe to call repeatedly: only one handler is ever attached per logger name.
    """
    buffer = get_buffer(channel, capacity=capacity)
    target = logging.getLogger(logger_name)
    with _LOCK:
        if logger_name in _HANDLERS:
            return buffer
        handler = _BufferHandler(buffer)
        target.addHandler(handler)
        _HANDLERS[logger_name] = handler
    return buffer


def reset() -> None:
    """Detach every handler and drop all buffers (used by tests)."""
    with _LOCK:
        for logger_name, handler in _HANDLERS.items():
            logging.getLogger(logger_name).removeHandler(handler)
        _HANDLERS.clear()
        _BUFFERS.clear()
