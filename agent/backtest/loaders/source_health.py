"""Process-local loader health so fallback chains prefer reachable hosts.

Public quote endpoints (Tencent / Eastmoney / AkShare, …) often time out or
drop connections under IP pressure. Monitor scans fetch many symbols one by
one; without memory of recent failures every symbol pays another 15s timeout
on the same dead host before walking the rest of the chain.

This module keeps a tiny per-source score + circuit breaker in-process:

* consecutive transport failures open a short cooldown (skip that source);
* a success clears the breaker and boosts the source's rank so later fetches
  try the working host first;
* ties preserve the static ``FALLBACK_CHAINS`` order.

All state is best-effort and process-local — it does not coordinate across
machines or persist across restarts.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

# After this many consecutive transport failures, skip the source for a while.
_FAIL_THRESHOLD = 3
_COOLDOWN_S = 180.0
# 501 / RST / aborted keep-alive: the host is refusing us; do not burn the
# rest of a multi-thousand-symbol scan waiting for the same error.
_HARD_FAIL_MARKERS = (
    "501",
    "not implemented",
    "connection aborted",
    "remotedisconnected",
    "connection reset",
    "10054",
    "remotely closed",
)
# Recent successes outweigh the static chain order; the boost fades so a
# temporarily-lucky host does not stay pinned forever after it recovers poorly.
_SUCCESS_BOOST = 10.0
_SUCCESS_HALF_LIFE_S = 300.0


@dataclass
class _SourceState:
    consec_fails: int = 0
    open_until: float = 0.0
    # Decaying score bumped on each success; used only for relative ordering.
    success_score: float = 0.0
    score_updated_at: float = field(default_factory=time.monotonic)


_LOCK = threading.Lock()
_STATE: dict[str, _SourceState] = {}


def reset_for_tests() -> None:
    """Clear all health state (test helper only)."""
    with _LOCK:
        _STATE.clear()


def is_cooling_down(source: str) -> bool:
    """True while *source*'s circuit is open (recent transport failures)."""
    key = _norm(source)
    if not key:
        return False
    with _LOCK:
        state = _STATE.get(key)
        if state is None:
            return False
        return time.monotonic() < state.open_until


def record_success(source: str) -> None:
    """Mark *source* reachable: clear breaker and boost its preference score."""
    key = _norm(source)
    if not key:
        return
    now = time.monotonic()
    with _LOCK:
        state = _STATE.setdefault(key, _SourceState())
        state.consec_fails = 0
        state.open_until = 0.0
        state.success_score = _decayed_score(state, now) + _SUCCESS_BOOST
        state.score_updated_at = now


def record_failure(source: str, exc: BaseException | None = None) -> None:
    """Record a transport failure; open the circuit after the threshold.

    HTTP 501 / RemoteDisconnected open immediately so a dead public quote host
    is not retried for every remaining symbol in the same fetch().
    """
    key = _norm(source)
    if not key:
        return
    now = time.monotonic()
    hard = _is_hard_failure(exc)
    with _LOCK:
        state = _STATE.setdefault(key, _SourceState())
        if hard:
            state.consec_fails = max(state.consec_fails, _FAIL_THRESHOLD)
        else:
            state.consec_fails += 1
        fails = state.consec_fails
        opened = False
        if fails >= _FAIL_THRESHOLD and now >= state.open_until:
            state.open_until = now + _COOLDOWN_S
            opened = True
    if opened:
        logger.warning(
            "loader source %r circuit opened for %.0fs after %d failures%s",
            key,
            _COOLDOWN_S,
            fails,
            f" (last error: {exc})" if exc is not None else "",
        )


def _is_hard_failure(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _HARD_FAIL_MARKERS)


def prefer_order(sources: Sequence[str]) -> list[str]:
    """Reorder *sources* so healthy / recently-successful hosts come first.

    Cooling-down sources are pushed to the end but still returned — callers
    that want a hard skip should check :func:`is_cooling_down` separately.
    Relative order among equal-rank sources is preserved (stable sort).
    """
    now = time.monotonic()
    with _LOCK:
        scored: list[tuple[int, float, int, str]] = []
        for index, name in enumerate(sources):
            key = _norm(name)
            state = _STATE.get(key)
            cooling = 1 if state is not None and now < state.open_until else 0
            score = _decayed_score(state, now) if state is not None else 0.0
            # Sort key: healthy first, then higher score, then original index.
            scored.append((cooling, -score, index, name))
    scored.sort()
    return [name for _, _, _, name in scored]


def ordered_fallback_chain(chain: Sequence[str]) -> list[str]:
    """Alias for :func:`prefer_order` kept for call-site readability."""
    return prefer_order(chain)


def _norm(source: str) -> str:
    return str(source or "").strip().lower()


def _decayed_score(state: _SourceState | None, now: float) -> float:
    if state is None or state.success_score <= 0:
        return 0.0
    age = max(0.0, now - state.score_updated_at)
    if age <= 0:
        return state.success_score
    # Exponential decay with the configured half-life.
    return state.success_score * (0.5 ** (age / _SUCCESS_HALF_LIFE_S))
