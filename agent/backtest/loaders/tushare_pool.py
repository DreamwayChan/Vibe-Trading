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

import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
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

# Endpoints whose real budget is a handful of calls *per day*, far below the
# RPM cap. A 120-point account gets ``stk_mins`` at 2 calls/day; cooling 65s and
# walking the token list then burns every account's whole day in one request.
# Values are per token and per Beijing calendar day.
_API_DAILY_QUOTA: dict[str, int] = {"stk_mins": 2}

# Endpoints whose per-minute allowance is *below* the account-wide RPM. The
# same ``stk_mins`` also answers "频率超限(1次/分钟)": the sliding window has to
# pace this endpoint separately or every second request is rejected.
_API_MIN_INTERVAL_S: dict[str, float] = {"stk_mins": 61.0}

_DAILY_LIMIT_MARKERS = ("次/天", "/天", "per day", "每日", "每天", "daily limit")
_MINUTE_LIMIT_MARKERS = ("次/分钟", "/分钟", "次/分", "per minute")

# ``stk_mins`` errors say "频率超限(2次/天)" — daily resets are wall clock in
# Beijing, not process-relative, so they need a real timestamp, not monotonic.
_CN_TZ = dt.timezone(dt.timedelta(hours=8))

_TOKEN_SPLIT_RE = re.compile(r"[,;\n]+")

_pool_lock = threading.Lock()
_pool_singleton: Optional["TushareClientPool"] = None
_http_lock = threading.Lock()
_http_session: requests.Session | None = None
_quota_lock = threading.Lock()


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


def is_daily_quota_error(exc: BaseException) -> bool:
    """True when Tushare's quota for this endpoint is a per-*day* allowance.

    ``stk_mins`` on a 120-point account answers ``40203 频率超限(2次/天)``. That
    is not an RPM breach: cooling 65 seconds and retrying just spends the next
    token's daily allowance too, and one bulk request can exhaust every account
    for the rest of the day.
    """
    if not is_rate_limit_error(exc):
        return False
    message = str(exc)
    lowered = message.lower()
    return any(marker in message for marker in _DAILY_LIMIT_MARKERS) or "per day" in lowered


def daily_quota_for(api_name: str) -> int | None:
    """Per-token per-day call allowance for *api_name* (None = only RPM-bound)."""
    return _API_DAILY_QUOTA.get(str(api_name))


def min_interval_for(api_name: str) -> float:
    """Minimum seconds between two calls of *api_name* on one token."""
    return float(_API_MIN_INTERVAL_S.get(str(api_name), 0.0))


def is_per_minute_limit(exc: BaseException) -> bool:
    """True when the rejection is a sub-RPM per-minute cap ("1次/分钟")."""
    if not is_rate_limit_error(exc):
        return False
    message = str(exc)
    lowered = message.lower()
    return any(marker in message for marker in _MINUTE_LIMIT_MARKERS) or "per minute" in lowered


def seconds_until_quota_reset(when: dt.datetime | None = None) -> float:
    """Seconds until the next Beijing midnight, when daily quotas reset."""
    now = when or dt.datetime.now(_CN_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_CN_TZ)
    tomorrow = dt.datetime.combine(
        now.astimezone(_CN_TZ).date() + dt.timedelta(days=1), dt.time.min, tzinfo=_CN_TZ
    )
    return max((tomorrow - now.astimezone(_CN_TZ)).total_seconds(), 0.0)


def _quota_ledger_path() -> "Path":
    """Location of the cross-process daily-quota ledger."""
    try:
        from src.data.lake import lake_root

        root = lake_root()
    except Exception:  # noqa: BLE001 — backtest must not depend on the lake pkg
        root = Path.home() / ".vibe-trading" / "lake"
    return Path(root) / "tushare_quota.json"


def _beijing_day() -> str:
    return dt.datetime.now(_CN_TZ).date().isoformat()


def load_quota_ledger() -> dict[str, dict[str, int]]:
    """Read today's spend per token hint and endpoint (empty when stale/missing).

    Persisted so that a restart — or the next CLI invocation — does not have to
    re-discover an exhausted allowance by burning requests.
    """
    path = _quota_ledger_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt ledger is just empty
        return {}
    if not isinstance(payload, dict) or payload.get("day") != _beijing_day():
        return {}
    spent = payload.get("spent")
    if not isinstance(spent, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for hint, per_api in spent.items():
        if isinstance(per_api, dict):
            out[str(hint)] = {
                str(api): int(count)
                for api, count in per_api.items()
                if isinstance(count, (int, float))
            }
    return out


def record_quota_spend(token_hint: str, api_name: str, count: int = 1) -> None:
    """Add *count* spent calls to the ledger, atomically and best-effort."""
    if not token_hint or not api_name:
        return
    path = _quota_ledger_path()
    day = _beijing_day()
    with _quota_lock:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            payload = {"day": day, "spent": {}}
        if payload.get("day") != day:
            payload = {"day": day, "spent": {}}
        spent = payload.setdefault("spent", {})
        per_api = spent.setdefault(token_hint, {})
        per_api[api_name] = int(per_api.get(api_name, 0)) + int(count)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 — quota bookkeeping is advisory
            logger.debug("tushare quota ledger write failed: %s", exc)


class TushareAllTokensBlocked(RuntimeError):
    """Raised when every token is cooling on this endpoint right now.

    Distinct from :class:`TushareQuotaExhausted`: the day may still have budget,
    the tokens are just inside a per-hour/per-minute block.
    """

    def __init__(self, api_name: str, wait_seconds: float) -> None:
        self.api_name = api_name
        self.wait_seconds = wait_seconds
        super().__init__(
            f"tushare {api_name} blocked on all tokens; earliest slot free in "
            f"{wait_seconds:.0f}s"
        )


class TushareQuotaExhausted(RuntimeError):
    """Raised when every token's daily allowance for an endpoint is spent."""

    def __init__(self, api_name: str, reset_in: float) -> None:
        self.api_name = api_name
        self.reset_in = reset_in
        super().__init__(
            f"tushare {api_name} daily quota exhausted on all tokens; "
            f"resets in {reset_in / 3600:.1f}h (Beijing midnight)"
        )


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
        # Per-endpoint *daily* spend (stk_mins 2/day on 120-point accounts).
        self._api_daily_spent: dict[str, int] = {}
        # Wall-clock (monotonic) of the last hit per endpoint, for sub-RPM caps.
        self._api_last_hit: dict[str, float] = {}
        self._quota_day = _beijing_day()
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

    def _roll_quota_day(self) -> None:
        """Forget yesterday's spend once the Beijing date changes."""
        today = _beijing_day()
        if today != self._quota_day:
            self._quota_day = today
            self._api_daily_spent.clear()

    def daily_left(self, api_name: str | None) -> int | None:
        """Remaining calls today for *api_name* (None when not daily-capped)."""
        if not api_name or daily_quota_for(api_name) is None:
            return None
        self._roll_quota_day()
        return max(daily_quota_for(api_name) - self._api_daily_spent.get(api_name, 0), 0)

    def mark_daily_spent(self, api_name: str, count: int = 1) -> None:
        """Charge one call against today's allowance (persisted to the ledger)."""
        if daily_quota_for(api_name) is None:
            return
        self._roll_quota_day()
        self._api_daily_spent[api_name] = self._api_daily_spent.get(api_name, 0) + count
        record_quota_spend(self.quota_key(), api_name, count)

    def mark_daily_exhausted(self, api_name: str) -> None:
        """Server said the day is spent: stop offering this token for the endpoint."""
        quota = daily_quota_for(api_name)
        if quota is None:
            return
        self._roll_quota_day()
        already = self._api_daily_spent.get(api_name, 0)
        self._api_daily_spent[api_name] = quota
        if quota > already:
            record_quota_spend(self.quota_key(), api_name, quota - already)
        self.mark_api_cooldown(api_name, seconds_until_quota_reset())

    def seed_daily_spent(self, per_api: dict[str, int]) -> None:
        """Adopt spend recorded by an earlier process on the same day."""
        if not per_api:
            return
        self._roll_quota_day()
        for api_name, count in per_api.items():
            self._api_daily_spent[api_name] = max(
                self._api_daily_spent.get(api_name, 0), int(count)
            )

    def wait_seconds(self, now: float, api_name: str | None = None) -> float:
        """Seconds until this slot may fire again (0 means ready now)."""
        # An endpoint can be capped below the account-wide RPM (stk_mins 1/min),
        # so its own spacing floor wins over the shared sliding window.
        api_wait = max(
            self.api_wait_seconds(now, api_name),
            self.interval_wait_seconds(now, api_name),
        )
        if now < self.cooldown_until:
            return max(self.cooldown_until - now, api_wait)
        self._prune(now)
        if len(self.hits) < self.rpm:
            return api_wait
        rpm_wait = max(self.hits[0] + self.window_seconds - now, 0.0)
        return max(rpm_wait, api_wait)

    def interval_wait_seconds(self, now: float, api_name: str | None) -> float:
        """Seconds left before this endpoint's minimum spacing has elapsed."""
        if not api_name:
            return 0.0
        floor = min_interval_for(api_name)
        if floor <= 0:
            return 0.0
        last = self._api_last_hit.get(api_name)
        if last is None:
            return 0.0
        return max(floor - (now - last), 0.0)

    def mark_api_hit(self, api_name: str | None, now: float | None = None) -> None:
        """Start the minimum-spacing clock for *api_name*."""
        if not api_name or min_interval_for(api_name) <= 0:
            return
        self._api_last_hit[api_name] = now if now is not None else time.monotonic()

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

    def quota_key(self) -> str:
        """Stable, non-secret ledger key for this token.

        ``token_hint`` collapses short tokens to ``***``, which would merge
        distinct accounts into one ledger row and silently under-count spend.
        A truncated digest stays unique without putting the token on disk.
        """
        digest = hashlib.sha1(self.token.encode("utf-8")).hexdigest()
        return f"k{digest[:12]}"


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
        # Adopt what earlier runs already spent today, so a restart does not
        # rediscover an exhausted 2-calls/day allowance by wasting requests.
        ledger = load_quota_ledger()
        for slot in self._slots:
            slot.seed_daily_spent(ledger.get(slot.quota_key(), {}))
        self._lock = threading.Lock()
        # Round-robin cursor used only to break ties among equally ready slots.
        self._rr = 0

    @property
    def token_count(self) -> int:
        return len(self._slots)

    def has_tokens(self) -> bool:
        return bool(self._slots)

    def earliest_wait(self, api_name: str | None = None) -> float:
        """Shortest wait across tokens, ignoring the fail-fast skip.

        Used for diagnostics: "every token is cooling, next chance in N s" is
        very different from "the day is spent".
        """
        now = time.monotonic()
        waits = [slot.wait_seconds(now, api_name) for slot in self._slots]
        return min(waits) if waits else 0.0

    def quota_report(self) -> dict[str, dict[str, int]]:
        """Remaining allowance for every day-capped endpoint (RPM-only ones omitted).

        Returns:
            ``{api: {"per_token": n, "tokens": m, "remaining": k}}`` where
            ``remaining`` is summed across tokens for the current Beijing day.
        """
        report: dict[str, dict[str, int]] = {}
        for api_name, per_token in _API_DAILY_QUOTA.items():
            report[api_name] = {
                "per_token": per_token,
                "tokens": len(self._slots),
                "remaining": sum(slot.daily_left(api_name) or 0 for slot in self._slots),
            }
        return report

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
            # Day-capped endpoints (stk_mins 2/day) must be skipped outright:
            # their "cooldown" is until midnight, so waiting is pointless.
            if slot.daily_left(api_name) == 0:
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
                    slot.mark_api_hit(api_name, now)
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
                if daily_quota_for(api_name) is not None and all(
                    s.daily_left(api_name) == 0 for s in self._slots
                ):
                    raise TushareQuotaExhausted(api_name, seconds_until_quota_reset())
                if last_exc is not None:
                    raise last_exc
                raise TushareAllTokensBlocked(api_name, self.earliest_wait(api_name))
            try:
                result = slot.invoke(api_name, **kwargs)
            except Exception as exc:  # noqa: BLE001 - Tushare raises varied types
                if is_rate_limit_error(exc):
                    last_exc = exc
                    if is_daily_quota_error(exc):
                        # 40203 "2次/天": walking to the next token just spends
                        # its allowance too. Mark it spent and move on once.
                        slot.mark_daily_exhausted(api_name)
                        logger.warning(
                            "tushare daily quota spent on token %s for %s "
                            "(no retry until Beijing midnight): %.120s",
                            slot.token_hint(),
                            api_name,
                            exc,
                        )
                    elif is_hourly_rate_limit(exc):
                        # Keep daily() usable; only this endpoint is spent.
                        slot.mark_api_cooldown(api_name, _HOURLY_COOLDOWN_S)
                        logger.warning(
                            "tushare hourly quota on token %s for %s; "
                            "skipping this token for %.0fs (no minute cooldown)",
                            slot.token_hint(),
                            api_name,
                            _HOURLY_COOLDOWN_S,
                        )
                    elif is_per_minute_limit(exc):
                        # Sub-RPM endpoint cap (stk_mins 1/min): wait out the
                        # interval instead of the generic one-minute cooldown.
                        wait_s = max(min_interval_for(api_name), self._cooldown_seconds)
                        slot.mark_cooldown(wait_s)
                        logger.warning(
                            "tushare per-minute cap on token %s for %s; waiting %.0fs",
                            slot.token_hint(),
                            api_name,
                            wait_s,
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
            # A successful call is what consumes a day-capped allowance; the
            # rejected 40203 attempts above do not.
            slot.mark_daily_spent(api_name)
            return result

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
