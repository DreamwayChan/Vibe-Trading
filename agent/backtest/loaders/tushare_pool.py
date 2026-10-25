"""Process-wide Tushare multi-token client pool with per-token rate limiting.

Tushare Pro accounts are typically capped at ~50 calls/minute. Large agent
backtests and universe scans exceed that quickly when every symbol is fetched
one-by-one. This module:

1. Resolves one or more tokens from ``TUSHARE_TOKEN`` / ``TUSHARE_TOKENS``
2. Schedules each call onto the token that can fire soonest (least-wait)
3. Enforces a sliding-window RPM budget per token before the request leaves
4. On a server-side frequency error, cools that token and retries on another

All Tushare HTTP traffic in the process should go through
:func:`get_tushare_pool` / :meth:`TushareClientPool.call` so quotas stay shared
across loaders, fundamentals, and fallback adapters.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from collections import deque
from typing import Any, Deque, List, Optional, Sequence

import pandas as pd
import requests

logger = logging.getLogger(__name__)

TUSHARE_TOKEN_PLACEHOLDERS = frozenset({"", "your-tushare-token"})

# Match both the Chinese quota message and common English variants.
_RATE_LIMIT_MARKERS = ("频率超限", "超过访问频率", "freq limit", "rate limit", "频率限制")

# Tushare's keep-alive session is often reset mid-scan (Windows 10054, empty
# JSON body). Retry these without burning the token's rate-limit cooldown.
_TRANSPORT_MARKERS = (
    "connection aborted",
    "connection reset",
    "remotely closed",
    "remote host",
    "10054",
    "broken pipe",
    "expecting value",
    "remotedisconnected",
    "connectionreseterror",
    "read timed out",
    "max retries exceeded",
    "empty http body",
    "non-json from",
)
_TRANSPORT_EXC_NAMES = frozenset(
    {
        "JSONDecodeError",
        "ProtocolError",
        "RemoteDisconnected",
        "IncompleteRead",
        "ChunkedEncodingError",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "ProxyError",
        "SSLError",
    }
)
_MAX_TRANSPORT_RETRIES = 3

# Slightly longer than one minute so a cooled token is not immediately re-hit.
_DEFAULT_COOLDOWN_S = 65.0
# ``index_daily`` is 1 call/hour on many plans. Waiting the RPM cooldown then
# retrying the same quota stalls forecast for minutes and still fails.
_HOURLY_COOLDOWN_S = 3600.0
# Do not sleep on an endpoint whose remaining block exceeds this; fail through
# to eastmoney/tencent instead of parking the request for an hour.
_API_BLOCK_FAIL_FAST_S = 90.0

_TOKEN_SPLIT_RE = re.compile(r"[,;\n]+")

_pool_lock = threading.Lock()
_pool_singleton: Optional["TushareClientPool"] = None
_http_lock = threading.Lock()
_http_session: requests.Session | None = None


def parse_token_list(raw: str) -> List[str]:
    """Split a comma / semicolon / newline separated token string."""
    if not raw or not str(raw).strip():
        return []
    parts = _TOKEN_SPLIT_RE.split(str(raw))
    return [part.strip() for part in parts if part.strip()]


def resolve_tushare_tokens(data_config: Any | None = None) -> List[str]:
    """Return unique configured tokens (primary + extras), placeholders removed.

    ``TUSHARE_TOKEN`` is listed first for backward compatibility; extras come
    from ``TUSHARE_TOKENS``. Duplicates are dropped while preserving order.
    """
    if data_config is None:
        from src.config.accessor import get_env_config

        data_config = get_env_config().data

    primary = getattr(data_config, "tushare_token", "") or ""
    extras = getattr(data_config, "tushare_tokens", "") or ""
    ordered = parse_token_list(primary) + parse_token_list(extras)

    seen: set[str] = set()
    unique: List[str] = []
    for token in ordered:
        if token in TUSHARE_TOKEN_PLACEHOLDERS or token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def resolve_tushare_rpm(data_config: Any | None = None) -> int:
    """Per-token requests-per-minute budget (default 50)."""
    if data_config is None:
        from src.config.accessor import get_env_config

        data_config = get_env_config().data
    rpm = int(getattr(data_config, "tushare_rpm", 50) or 50)
    return max(rpm, 1)


_DEFAULT_API_URL = "https://api.tushare.pro"
_DEFAULT_TIMEOUT = 30
# DataApi name-mangles __http_url / __timeout; older builds used a public attr.
_API_URL_ATTRS = ("_DataApi__http_url", "_http_url")
_TIMEOUT_ATTRS = ("_DataApi__timeout", "_timeout")


def resolve_tushare_api_url(data_config: Any | None = None) -> str:
    """Tushare HTTP endpoint. HTTPS avoids intranet blocks on HTTP/80."""
    if data_config is None:
        from src.config.accessor import get_env_config

        data_config = get_env_config().data
    url = str(getattr(data_config, "tushare_api_url", "") or "").strip()
    return url or _DEFAULT_API_URL


def resolve_tushare_timeout(data_config: Any | None = None) -> int:
    """Per-request timeout in seconds (SDK default is 10)."""
    if data_config is None:
        from src.config.accessor import get_env_config

        data_config = get_env_config().data
    timeout = int(getattr(data_config, "tushare_timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    return max(timeout, 1)


def bind_tushare_client(pro: Any, *, api_url: str, timeout: int) -> None:
    """Point a ``DataApi`` instance at our URL/timeout (SDK hardcodes HTTP/80)."""
    for attr in _API_URL_ATTRS:
        try:
            setattr(pro, attr, api_url)
        except Exception:
            continue
    for attr in _TIMEOUT_ATTRS:
        try:
            setattr(pro, attr, timeout)
        except Exception:
            continue


def reset_tushare_http_session() -> None:
    """Drop the shared requests session (call after a reset / poisoned keep-alive)."""
    global _http_session
    with _http_lock:
        session = _http_session
        _http_session = None
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def tushare_http_session() -> requests.Session:
    """Session that follows Windows system proxies the way ``curl.exe`` does.

    ``requests`` only reads ``HTTP(S)_PROXY`` env vars. On intranet PCs curl
    often works because it uses WinINET, while the SDK POST goes direct (or
    through a different proxy) and comes back as an empty ``daily`` frame.
    """
    global _http_session
    with _http_lock:
        if _http_session is None:
            session = requests.Session()
            try:
                system_proxies = {
                    scheme: proxy
                    for scheme, proxy in urllib.request.getproxies().items()
                    if proxy
                }
            except Exception:
                system_proxies = {}
            if system_proxies:
                session.proxies.update(system_proxies)
                logger.info(
                    "tushare HTTP using system proxy schemes: %s",
                    sorted(system_proxies),
                )
            _http_session = session
        return _http_session


def post_tushare_api(token: str, api_name: str, **kwargs: Any) -> pd.DataFrame:
    """POST JSON to Tushare Pro and return a DataFrame (SDK-compatible).

    Raises on HTTP/JSON failures and on non-zero Tushare ``code`` so a
    permission or body-stripped response is not mistaken for “no bars”.
    """
    fields = kwargs.pop("fields", "") or ""
    url = resolve_tushare_api_url()
    timeout = resolve_tushare_timeout()
    payload = {
        "api_name": api_name,
        "token": token,
        "params": kwargs,
        "fields": fields,
    }
    response = tushare_http_session().post(url, json=payload, timeout=timeout)
    text = response.text or ""
    if not str(text).strip():
        raise ValueError(
            f"tushare empty HTTP body from {url} (http {response.status_code})"
        )
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = str(text)[:180].replace("\n", " ")
        raise ValueError(
            f"tushare non-JSON from {url} (http {response.status_code}): {preview}"
        ) from exc
    if not isinstance(result, dict):
        raise ValueError(
            f"tushare unexpected JSON from {url}: {type(result).__name__}"
        )

    api_code = result.get("code")
    api_msg = str(result.get("msg") or "")
    if api_code not in (0, "0", None):
        raise RuntimeError(f"tushare {api_name} error {api_code}: {api_msg}")

    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError(
            f"tushare {api_name} error: data is {type(data).__name__} ({api_msg})"
        )
    columns = data.get("fields") or []
    items = data.get("items") or []
    if not items:
        logger.warning(
            "tushare %s returned 0 rows from %s (http %s api_code=%s msg=%r params=%s)",
            api_name,
            url,
            response.status_code,
            api_code,
            api_msg,
            {k: v for k, v in kwargs.items() if k != "token"},
        )
    return pd.DataFrame(items, columns=columns)


def is_rate_limit_error(exc: BaseException) -> bool:
    """Detect Tushare frequency / rate-limit failures from exception text."""
    message = str(exc)
    lowered = message.lower()
    for marker in _RATE_LIMIT_MARKERS:
        if marker.lower() in lowered or marker in message:
            return True
    return False


def is_hourly_rate_limit(exc: BaseException) -> bool:
    """True when Tushare's remaining quota is per-hour (e.g. index_daily 1/hour)."""
    if not is_rate_limit_error(exc):
        return False
    message = str(exc)
    lowered = message.lower()
    return "次/小时" in message or "/小时" in message or "per hour" in lowered


def is_transport_error(exc: BaseException) -> bool:
    """Detect dropped keep-alive / empty-body failures that deserve a retry.

    Tushare's ``requests`` session reuses TCP connections. After a burst of
    ``daily()`` calls the peer often sends RST (Windows 10054) or returns an
    empty body that ``json.loads`` reports as ``Expecting value: line 1``.
    Those are not quota errors — a fresh session usually succeeds.
    """
    if isinstance(
        exc,
        (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError),
    ):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10054:
        return True
    if type(exc).__name__ in _TRANSPORT_EXC_NAMES:
        return True
    lowered = str(exc).lower()
    return any(marker in lowered for marker in _TRANSPORT_MARKERS)


class _TokenSlot:
    """One credential with its own sliding-window RPM and cooldown clock."""

    def __init__(self, token: str, rpm: int, window_seconds: float = 60.0) -> None:
        self.token = token
        self.rpm = max(int(rpm), 1)
        self.window_seconds = float(window_seconds)
        self.hits: Deque[float] = deque()
        self.cooldown_until = 0.0
        # Per-endpoint blocks: index_daily 1/hour must not freeze daily().
        self._api_blocked_until: dict[str, float] = {}
        # requests.Session is not safe to share across overlapping HTTP calls.
        self._call_lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.hits and self.hits[0] < cutoff:
            self.hits.popleft()

    def api_wait_seconds(self, now: float, api_name: str | None) -> float:
        """Seconds until this token may call ``api_name`` again."""
        if not api_name:
            return 0.0
        blocked_until = self._api_blocked_until.get(api_name, 0.0)
        if now < blocked_until:
            return blocked_until - now
        return 0.0

    def wait_seconds(self, now: float, api_name: str | None = None) -> float:
        """Seconds until this slot may fire again (0 means ready now)."""
        api_wait = self.api_wait_seconds(now, api_name)
        if now < self.cooldown_until:
            return max(self.cooldown_until - now, api_wait)
        self._prune(now)
        if len(self.hits) < self.rpm:
            return api_wait
        rpm_wait = max(self.hits[0] + self.window_seconds - now, 0.0)
        return max(rpm_wait, api_wait)

    def record(self, now: float) -> None:
        self._prune(now)
        self.hits.append(now)

    def mark_cooldown(self, seconds: float = _DEFAULT_COOLDOWN_S) -> None:
        self.cooldown_until = time.monotonic() + max(float(seconds), 0.0)

    def mark_api_cooldown(self, api_name: str, seconds: float) -> None:
        """Block only this endpoint on this token (hourly index_daily, etc.)."""
        if not api_name:
            return
        self._api_blocked_until[api_name] = time.monotonic() + max(float(seconds), 0.0)

    def invalidate_client(self) -> None:
        """Drop the shared HTTP session so the next call opens a new connection."""
        reset_tushare_http_session()

    def invoke(self, api_name: str, **kwargs: Any) -> Any:
        """Call one Tushare method, serializing HTTP on this token's session."""
        with self._call_lock:
            try:
                return post_tushare_api(self.token, api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - Tushare raises varied types
                if is_transport_error(exc):
                    self.invalidate_client()
                raise

    def token_hint(self) -> str:
        """Short non-secret suffix for logs."""
        token = self.token
        if len(token) <= 8:
            return "***"
        return f"...{token[-4:]}"


class TushareClientPool:
    """Thread-safe multi-token Tushare gateway with least-wait scheduling."""

    def __init__(
        self,
        tokens: Sequence[str] | None = None,
        *,
        rpm: int | None = None,
        window_seconds: float = 60.0,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_S,
    ) -> None:
        resolved = list(tokens) if tokens is not None else resolve_tushare_tokens()
        per_token_rpm = int(rpm) if rpm is not None else resolve_tushare_rpm()
        self._window_seconds = float(window_seconds)
        self._cooldown_seconds = float(cooldown_seconds)
        self._slots = [
            _TokenSlot(token, per_token_rpm, self._window_seconds) for token in resolved
        ]
        self._lock = threading.Lock()
        # Round-robin cursor used only to break ties among equally ready slots.
        self._rr = 0

    @property
    def token_count(self) -> int:
        return len(self._slots)

    def has_tokens(self) -> bool:
        return bool(self._slots)

    def _pick_ready_slot(
        self,
        now: float,
        *,
        api_name: str | None = None,
    ) -> tuple[float, int, _TokenSlot] | None:
        """Return ``(wait_s, index, slot)`` for the earliest-available token.

        Tokens whose remaining block on ``api_name`` is longer than
        ``_API_BLOCK_FAIL_FAST_S`` are skipped so an hourly index_daily quota
        does not park the caller for 65s×N before eastmoney can run.
        """
        if not self._slots:
            raise RuntimeError("No Tushare tokens configured")

        n = len(self._slots)
        best_wait: float | None = None
        best_idx = 0
        best_slot: _TokenSlot | None = None

        # Walk from the RR cursor so equally ready tokens share load fairly.
        for offset in range(n):
            idx = (self._rr + offset) % n
            slot = self._slots[idx]
            api_wait = slot.api_wait_seconds(now, api_name)
            if api_wait > _API_BLOCK_FAIL_FAST_S:
                continue
            wait = slot.wait_seconds(now, api_name)
            if best_wait is None or wait < best_wait:
                best_wait = wait
                best_idx = idx
                best_slot = slot
                if wait <= 0:
                    break

        if best_wait is None or best_slot is None:
            return None
        return best_wait, best_idx, best_slot

    def acquire(self, api_name: str | None = None) -> _TokenSlot | None:
        """Reserve one RPM hit. None means every token is blocked on this API."""
        while True:
            with self._lock:
                now = time.monotonic()
                picked = self._pick_ready_slot(now, api_name=api_name)
                if picked is None:
                    return None
                wait_s, idx, slot = picked
                if wait_s <= 0:
                    slot.record(now)
                    self._rr = (idx + 1) % max(len(self._slots), 1)
                    return slot
                sleep_for = min(wait_s, 1.0)
            # Sleep outside the lock so other callers can progress on other tokens.
            time.sleep(sleep_for)

    def call(self, api_name: str, **kwargs: Any) -> Any:
        """Invoke ``pro.<api_name>(**kwargs)`` with scheduling and retry."""
        if not self._slots:
            raise RuntimeError("No Tushare tokens configured")

        last_exc: BaseException | None = None
        transport_retries = 0
        # One attempt per token, plus a final pass after cooldowns may clear,
        # plus a few extra tries for dropped keep-alive connections.
        max_attempts = max(len(self._slots) * 2, 1) + _MAX_TRANSPORT_RETRIES
        for _ in range(max_attempts):
            slot = self.acquire(api_name)
            if slot is None:
                if last_exc is not None:
                    raise last_exc
                raise RuntimeError(
                    f"tushare {api_name} hourly quota exhausted on all tokens"
                )
            try:
                return slot.invoke(api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - Tushare raises varied types
                if is_rate_limit_error(exc):
                    last_exc = exc
                    if is_hourly_rate_limit(exc):
                        # Keep daily() usable; only this endpoint is spent.
                        slot.mark_api_cooldown(api_name, _HOURLY_COOLDOWN_S)
                        logger.warning(
                            "tushare hourly quota on token %s for %s; "
                            "skipping this token for %.0fs (no minute cooldown)",
                            slot.token_hint(),
                            api_name,
                            _HOURLY_COOLDOWN_S,
                        )
                    else:
                        slot.mark_cooldown(self._cooldown_seconds)
                        logger.warning(
                            "tushare rate-limited on token %s for %s; cooling %.0fs then retrying",
                            slot.token_hint(),
                            api_name,
                            self._cooldown_seconds,
                        )
                    continue
                if is_transport_error(exc) and transport_retries < _MAX_TRANSPORT_RETRIES:
                    transport_retries += 1
                    last_exc = exc
                    logger.warning(
                        "tushare transport error on token %s for %s via %s "
                        "(retry %d/%d): %s",
                        slot.token_hint(),
                        api_name,
                        resolve_tushare_api_url(),
                        transport_retries,
                        _MAX_TRANSPORT_RETRIES,
                        exc,
                    )
                    time.sleep(min(0.5 * transport_retries, 2.0))
                    continue
                raise

        assert last_exc is not None
        raise last_exc


class PoolApiProxy:
    """Attribute proxy so existing ``self.api.daily(...)`` call sites stay intact.

    Tests that replace ``loader.api`` with a ``MagicMock`` continue to work
    because they overwrite this proxy entirely.
    """

    def __init__(self, pool: TushareClientPool) -> None:
        self._pool = pool

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _method(**kwargs: Any) -> Any:
            return self._pool.call(name, **kwargs)

        return _method


def get_tushare_pool() -> TushareClientPool:
    """Return the process singleton pool, creating it on first use."""
    global _pool_singleton
    with _pool_lock:
        if _pool_singleton is None:
            _pool_singleton = TushareClientPool()
        return _pool_singleton


def reset_tushare_pool() -> None:
    """Drop the singleton so the next call reloads tokens from env/config.

    Called after Settings writes new credentials into ``.env`` / ``os.environ``.
    """
    global _pool_singleton
    with _pool_lock:
        _pool_singleton = None
    reset_tushare_http_session()
