"""Eastmoney industry/concept lists and constituents, cached per trade date.

Board taxonomy is free push2 JSON (no Tushare points). Constituents are written
per BK so a half-finished first scan can resume without re-hitting every board.

Eastmoney rate-limits the ``clist`` path aggressively by source IP: a full
force-refresh burst (hundreds of boards x pages) can get the client's IP
temporarily banned on ``clist`` only — ``push2his`` klines and datacenter keep
working. When that happens every fetch here degrades to the most recent
successfully cached board list / member file (T-1 universe) instead of failing
the whole scan, as long as a cache from the last ``_MAX_CACHE_AGE_DAYS`` days
exists.

Call pressure is bounded on purpose: same-day member caches are reused even on
a forced scan, ``pz=1000`` big pages keep a full sweep to one call per board,
a process-wide adaptive gate spaces every clist call (scan and warm-up alike),
a per-scan request budget caps total calls, and a consecutive-failure breaker
stops all clist traffic with an escalating, disk-persisted cooldown once the
endpoint looks banned.

Probing is deliberately stingy. An elapsed cooldown arms exactly one
single-request probe, and a *failed* probe is not a new trip: it only doubles
the next quiet window (30min → 1h → 2h → 4h). Counting it as a trip reset the
escalation to its base every time, so a ban that outlives the app's cooldown —
the normal case — turned into a probe loop that re-extended the ban all day
(5 escalations in one afternoon, each one a fresh strike on the WAF). After
``VIBE_TRADING_MARKET_PICK_CLIST_PROBES`` failed probes in a day (default 4) the
endpoint is left alone until local midnight; the cached universe keeps serving.

Pacing knobs: ``VIBE_TRADING_EASTMONEY_MIN_INTERVAL``
(base HTTP spacing), ``VIBE_TRADING_MARKET_PICK_CLIST_GAP`` (process-wide clist
gate), ``VIBE_TRADING_MARKET_PICK_MAX_CLIST`` (per-scan budget),
``VIBE_TRADING_MARKET_PICK_CLIST_COOLDOWN`` (first-trip cooldown),
``VIBE_TRADING_MARKET_PICK_CLIST_PROBES`` (probes allowed per day).

Because that budget is far smaller than the ~1000-board taxonomy, coverage also
comes from :func:`bulk_fill_members`: one sweep of the whole A-share tape
carries each stock's industry and concept tags, filling hundreds of boards for
a handful of requests. The sweep is name-based, so it is only trusted after it
agrees with member files the per-board path already wrote; when it disagrees or
comes back truncated it is discarded and the slow per-board path takes over
(``VIBE_TRADING_MARKET_PICK_BULK_MIN_MISSING`` + ``..._BULK_CALLS`` tune it).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from backtest.loaders.eastmoney_client import get_json
from src.market_pick.store import market_pick_dir

logger = logging.getLogger(__name__)

BoardType = Literal["industry", "concept"]

# Realtime ``push2`` is often RST'd (WAF / CDN). Research scripts already
# prefer ``push2delay`` for the same clist path — try that first.
_CLIST_URLS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
# Sticky host for this process so a working delay node is not re-probed
# (and a dead push2 node is not re-hit) on every board.
_working_clist_url: str | None = None
# ``f6``/``f3`` (turnover, change) ride along for free so the warm-up can fill
# the boards people actually look at first instead of an arbitrary order.
_LIST_FIELDS = "f12,f14,f6,f3"
_MEMBER_FIELDS = "f12,f13,f14"

# One extra try per host after a short backoff: single RST blips happen even
# when the client is not banned, and a retry is much cheaper than losing the
# whole board universe for the day.
_RETRY_BACKOFF_S = 2.0
# Board-list / member caches older than this are considered stale and will not
# mask a fetch outage.
_MAX_CACHE_AGE_DAYS = 30

# --- Request-pressure guard -------------------------------------------------
# A full first-refresh of the day costs ~1 call per board (500+) plus the two
# taxonomy lists. Eastmoney bans ``clist`` by source IP when that runs hot, so
# five independent brakes sit in front of every clist call:
#
# 1. big pages — ``pz=1000`` pulls a whole board (or the whole taxonomy list)
#    in one call instead of paging 100 rows at a time, cutting the request
#    count per sweep by ~70%;
# 2. a consecutive-failure breaker — repeated dead calls trip it and the rest
#    of the scan (and any scan for the next cooldown) reads the on-disk cache
#    instead of re-hammering a banned endpoint. The cooldown *escalates* every
#    time it re-trips within one day (15m -> 30m -> 1h -> 2h ... capped at 8h)
#    and is persisted to disk, so neither repeated scans nor a process restart
#    can keep probing an IP that Eastmoney has banned;
# 3. probe economy — when a cooldown elapses the very next clist call is a
#    one-shot probe (no retry, no second host). A banned IP stays banned and
#    every wasted request can extend the ban, so a failed probe re-trips the
#    breaker immediately at the next escalation level for the cost of exactly
#    one request;
# 4. a hard per-scan request budget — whatever happens, one scan can never
#    issue more than ``_max_clist_requests`` calls;
# 5. a process-wide adaptive gate — every clist call (scan inline and the
#    background warm-up thread alike) reserves the next fire slot, so the two
#    can never stack into a burst. The gap doubles on every failure (capped at
#    60s) and decays back to the base pace on success.
_DEFAULT_MAX_CLIST_REQUESTS = 800
_MAX_CLIST_REQUESTS_ENV = "VIBE_TRADING_MARKET_PICK_MAX_CLIST"
_DEFAULT_CLIST_COOLDOWN_S = 900.0
_CLIST_COOLDOWN_ENV = "VIBE_TRADING_MARKET_PICK_CLIST_COOLDOWN"
_TRIP_AFTER_FAILURES = 3
# Escalation for repeat trips within one day: 900s * 2^n, capped at 8h.
_TRIP_ESCALATION_FACTOR = 2.0
_TRIP_COOLDOWN_MAX_S = 8 * 3600.0
# Failed cooldown probes allowed per local day. Each probe is one blocked
# request the WAF can count, so the budget — not the user — decides when to
# stop trying; once spent, clist stays closed until midnight off the cache.
_DEFAULT_MAX_PROBES = 4
_MAX_PROBES_ENV = "VIBE_TRADING_MARKET_PICK_CLIST_PROBES"

# Process-wide minimum spacing between clist calls (scan + warm-up combined).
_DEFAULT_CLIST_GAP_S = 8.0
_CLIST_GAP_ENV = "VIBE_TRADING_MARKET_PICK_CLIST_GAP"
_CLIST_GAP_MAX_S = 60.0

# Eastmoney's WAF bans the source IP after roughly 80 clist calls in a few
# minutes, so a per-board sweep can never finish the ~1000-board taxonomy — it
# dies around board 60-80 whatever the IP, which is why coverage used to stall
# at a few hundred boards. Coverage therefore comes from two places: the bulk
# all-market sweep (see below), and, for whatever it cannot name, a TTL model
# instead of "refetch daily": a board is only re-pulled when its cache is older
# than ``_member_ttl_days`` (default 14), filled by a slow background warm-up
# (~1 call every 8s) rather than inside a scan.
_DEFAULT_MEMBER_TTL_DAYS = 14
_MEMBER_TTL_ENV = "VIBE_TRADING_MARKET_PICK_MEMBER_TTL_DAYS"
_DEFAULT_WARM_PACE_S = 8.0
_WARM_PACE_ENV = "VIBE_TRADING_MARKET_PICK_WARM_PACE"
# Expired (TTL) boards are refreshed at most this many per warm-up run, so the
# day a batch of caches expires cannot turn into another 490-call burst.
_DEFAULT_WARM_EXPIRE_QUOTA = 40
_WARM_EXPIRE_QUOTA_ENV = "VIBE_TRADING_MARKET_PICK_WARM_DAILY"
# Cold boards (no cache at all) a single scan may pull inline. Keeps one scan at
# ~45 calls total (5 board lists + 40 members), well under the ~80-call burst
# that trips the WAF; the remaining cold boards are fetched by the warm-up.
_DEFAULT_COLD_CAP = 40
_COLD_CAP_ENV = "VIBE_TRADING_MARKET_PICK_COLD_CAP"
# Wall-clock budget for inline cold fills inside one scan. With the process-wide
# clist gap at 8s, a 40-board cold cap would block the scan request for 5+
# minutes; the budget caps the user-visible wait and leaves the rest to the
# background warm-up, which fills at the same safe pace.
_DEFAULT_COLD_SECONDS = 90.0
_COLD_SECONDS_ENV = "VIBE_TRADING_MARKET_PICK_COLD_SECONDS"

# --- Bulk membership sweep --------------------------------------------------
# The per-board path can never finish the taxonomy: the WAF allows a few dozen
# clist calls per window while there are ~1000 boards, so coverage stalls at
# whatever the last successful burst reached. One query over the whole A-share
# tape carries every stock's industry (``f100``) and its concept tags
# (``f103``), turning a 1000-call sweep into a handful. Names are matched
# against the board taxonomy, and the result is cross-checked against member
# files the per-board path already wrote — a sweep that disagrees with them is
# thrown away instead of poisoning the cache.
_BULK_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_BULK_FIELDS = "f12,f13,f14,f100,f103"
_BULK_PAGE_SIZE = 5000
_BULK_CALLS_ENV = "VIBE_TRADING_MARKET_PICK_BULK_CALLS"
_DEFAULT_BULK_CALLS = 6
_BULK_MIN_MISSING_ENV = "VIBE_TRADING_MARKET_PICK_BULK_MIN_MISSING"
_DEFAULT_BULK_MIN_MISSING = 25
_BULK_VALIDATE_SAMPLES = 12
_BULK_VALIDATE_FLOOR = 0.5
# A real mapping tags most of the tape; anything far below is a wrong field
# (blank/placeholder column) rather than a taxonomy.
_BULK_MIN_CODES = {"industry": 1500, "concept": 800}
# Share of the taxonomy a sweep must be able to name. A field that only carries
# the coarse level tags a small fraction of the fine-grained board list and is
# rejected here instead of silently mis-assigning constituents.
_BULK_MIN_COVERAGE = 0.4
# An incomplete sweep is retried at most this often within a day.
_BULK_RETRY_AFTER_S = 7200.0

# Per-scan counters (reset by :func:`load_board_universe`).
_clist_calls = 0
_consecutive_failures = 0
_cold_fetches = 0
# Set once the scan's inline cold-fill budget is spent, so the remaining boards
# are skipped silently instead of logging per board.
_cold_stopped = False
# Monotonic deadline until which clist is considered banned; survives scans so
# a user hammering "refresh" cannot re-trigger the ban — and survives process
# restarts via :func:`_load_trip_state`.
_tripped_until: float | None = None
_trip_state_loaded = False
# Escalation bookkeeping: how many times the breaker tripped today (resets on
# a new calendar day) — drives the doubling cooldown.
_trip_count = 0
_trip_day: date | None = None
# Failed cooldown probes today. Separate from ``_trip_count`` on purpose: a
# probe that fails proves the ban is still on, so it must lengthen the quiet
# window instead of restarting the escalation from the base cooldown.
_probe_fails = 0
# Probe economy: after a cooldown elapses, the next clist call is a one-shot
# probe; a failed probe re-trips the breaker for one request of cost.
_probe_pending = False

# Process-wide adaptive gate shared by the scan thread and the warm-up thread.
_gate_lock = threading.Lock()
# Serialises the post-cooldown probe: scan and warm-up can both find
# ``_probe_pending`` set, and two simultaneous probes are two strikes on a WAF
# that is already blocking us.
_probe_lock = threading.Lock()
_gap = _DEFAULT_CLIST_GAP_S
_next_fire = 0.0
# Monotonic start of the current scan (bounds inline cold fills by wall clock).
_scan_started_at: float | None = None


class ClistUnavailable(RuntimeError):
    """clist is cooling down or out of budget; callers must use the cache."""


def _max_clist_requests() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_MAX_CLIST_REQUESTS_ENV, _DEFAULT_MAX_CLIST_REQUESTS)


def _clist_cooldown_s() -> float:
    from backtest.loaders.base import positive_env_float

    return positive_env_float(_CLIST_COOLDOWN_ENV, _DEFAULT_CLIST_COOLDOWN_S)


def _max_probes_per_day() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_MAX_PROBES_ENV, _DEFAULT_MAX_PROBES)


def _seconds_until_midnight() -> float:
    """Quiet window used once the day's probe budget is spent."""
    now = datetime.now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max((midnight - now).total_seconds(), 60.0)


def _member_ttl_days() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_MEMBER_TTL_ENV, _DEFAULT_MEMBER_TTL_DAYS)


def _warm_pace_s() -> float:
    from backtest.loaders.base import positive_env_float

    return positive_env_float(_WARM_PACE_ENV, _DEFAULT_WARM_PACE_S)


def _warm_expire_quota() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_WARM_EXPIRE_QUOTA_ENV, _DEFAULT_WARM_EXPIRE_QUOTA)


def _cold_cap() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_COLD_CAP_ENV, _DEFAULT_COLD_CAP)


def _cold_budget_s() -> float:
    from backtest.loaders.base import positive_env_float

    return positive_env_float(_COLD_SECONDS_ENV, _DEFAULT_COLD_SECONDS)


def _cold_deadline_passed() -> bool:
    """Whether this scan has already spent its inline cold-fill time budget."""
    if _scan_started_at is None:
        return False
    return (time.monotonic() - _scan_started_at) > _cold_budget_s()


def _bulk_max_calls() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_BULK_CALLS_ENV, _DEFAULT_BULK_CALLS)


def _bulk_min_missing() -> int:
    from backtest.loaders.base import positive_env_int

    return positive_env_int(_BULK_MIN_MISSING_ENV, _DEFAULT_BULK_MIN_MISSING)


def _reset_clist_state() -> None:
    """Reset the per-scan counters (not the cooldown deadline — that is shared)."""
    global _clist_calls, _consecutive_failures, _cold_fetches, _cold_stopped
    _clist_calls = 0
    _consecutive_failures = 0
    _cold_fetches = 0
    _cold_stopped = False


def _clist_gap_s() -> float:
    from backtest.loaders.base import positive_env_float

    return positive_env_float(_CLIST_GAP_ENV, _DEFAULT_CLIST_GAP_S)


def _trip_state_path() -> Path:
    return market_pick_dir() / "clist_state.json"


def _load_trip_state() -> None:
    """Honor a breaker trip saved by an earlier process run.

    Monotonic deadlines die with the process, so the persisted trip is stored
    on the wall clock; whatever remains of it is re-armed here. The day's trip
    and failed-probe counters ride along, otherwise a restart would reset the
    escalation and let the app probe a banned IP all over again.
    """
    global _tripped_until, _trip_state_loaded, _trip_count, _trip_day, _probe_fails
    _trip_state_loaded = True
    path = _trip_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    try:
        remaining = float(raw.get("tripped_until_epoch") or 0.0) - time.time()
    except (TypeError, ValueError):
        remaining = 0.0
    if remaining > 0:
        _tripped_until = time.monotonic() + remaining
        logger.warning(
            "market-pick clist: honoring persisted ban cooldown for another "
            "%.0fs (from a previous run)",
            remaining,
        )
    try:
        _trip_count = int(raw.get("trips") or 0)
        _probe_fails = int(raw.get("probe_fails") or 0)
        _trip_day = date.fromisoformat(str(raw.get("day") or "")) if raw.get("day") else None
    except (TypeError, ValueError):
        pass


def _save_trip_state() -> None:
    """Persist the breaker and today's counters so a restart cannot reset them."""
    global _tripped_until
    until_epoch = (
        time.time() + (_tripped_until - time.monotonic())
        if _tripped_until is not None
        else time.time()
    )
    payload = {
        "tripped_until_epoch": until_epoch,
        "trips": _trip_count,
        "probe_fails": _probe_fails,
        "day": _trip_day.isoformat() if _trip_day else date.today().isoformat(),
    }
    try:
        path = _trip_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — persistence is best-effort
        logger.warning("market-pick clist: trip state not persisted: %s", exc)


def _roll_day() -> None:
    """Reset the per-day counters when the local calendar day turns over."""
    global _trip_day, _trip_count, _probe_fails
    today = date.today()
    if _trip_day != today:
        _trip_day = today
        _trip_count = 0
        _probe_fails = 0


def _trip_cooldown(*, probe_failure: bool = False) -> float:
    """Open the breaker with an escalating cooldown; persist it. Returns length.

    A ``probe_failure`` does not count as a new trip. A probe is sent only after
    the previous cooldown fully elapsed, and its failure means the ban outlived
    that window — so the next window must be *longer*, not the base cooldown
    again. Counting it as a trip restarted the ladder at 15min, which is how a
    single ban became five escalations in one afternoon.
    """
    global _tripped_until, _trip_count, _probe_fails
    _roll_day()
    if probe_failure:
        _probe_fails += 1
        steps = max(_trip_count - 1 + _probe_fails, 1)
    else:
        _trip_count += 1
        steps = _trip_count - 1
    base = min(_TRIP_COOLDOWN_MAX_S, _clist_cooldown_s() * _TRIP_ESCALATION_FACTOR**steps)
    cooldown = base * random.uniform(0.9, 1.1)
    _tripped_until = time.monotonic() + cooldown
    _save_trip_state()
    return cooldown


def _clist_resume_wait_s() -> float:
    """How long a waiter (warm-up) should sleep before the next probe."""
    base = _clist_cooldown_s() + 30
    if _tripped_until is None:
        return base
    return max(base, _tripped_until - time.monotonic() + 30)


def _gate_wait() -> None:
    """Reserve the next process-wide clist fire slot and sleep until it."""
    global _next_fire
    gap = _clist_gap_s()
    if gap <= 0:
        return
    with _gate_lock:
        fire_at = max(time.monotonic(), _next_fire)
        _next_fire = fire_at + gap
    sleep_for = fire_at - time.monotonic()
    if sleep_for > 0:
        time.sleep(sleep_for)


def _gate_note(ok: bool) -> None:
    """Adapt the process-wide clist gap: double on failure, decay on success."""
    global _gap, _next_fire
    with _gate_lock:
        base = _clist_gap_s()
        if ok:
            _gap = max(base, _gap * 0.8)
        else:
            _gap = min(_CLIST_GAP_MAX_S, max(base, _gap) * 2.0)
            # A failure means the endpoint is souring: push the next slot out
            # right away so concurrent callers cannot fire into the ban.
            _next_fire = max(_next_fire, time.monotonic() + _gap)


def _clist_ready() -> bool:
    """Whether clist may be contacted at all right now.

    An expired cooldown clears itself and arms the one-shot probe, so the next
    scan gets a single cheap probe round instead of re-tripping expensively —
    unless the day's probe budget is already spent, in which case the endpoint
    is left alone until midnight.
    """
    global _tripped_until, _consecutive_failures, _probe_pending, _trip_state_loaded
    if not _trip_state_loaded:
        _load_trip_state()
    _roll_day()
    if _tripped_until is None:
        return True
    if time.monotonic() < _tripped_until:
        return False
    budget = _max_probes_per_day()
    if _probe_fails >= budget:
        quiet = _seconds_until_midnight()
        _tripped_until = time.monotonic() + quiet
        _save_trip_state()
        logger.warning(
            "market-pick clist: %d failed probes today (budget %d); leaving the "
            "endpoint alone for another %.0fs and serving cached boards",
            _probe_fails,
            budget,
            quiet,
        )
        return False
    _tripped_until = None
    _consecutive_failures = 0
    _probe_pending = True
    logger.info("market-pick clist cooldown elapsed; probing again")
    return True


# Same event/hype boards the research screener drops — not a dual-track tape.
_EXCLUDE_NAME_SUBSTR = (
    "ST",
    "退市",
    "次新",
    "预盈预增",
    "昨日连板",
    "昨日涨停",
    "高送转",
    "股权转让",
    "举牌",
    "微盘股",
    "破净股",
)

_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
}


@dataclass
class BoardUniverse:
    """One Eastmoney board and the A-share codes currently listed on it."""

    bk: str
    name: str
    board_type: BoardType
    symbols: list[str] = field(default_factory=list)


def load_board_universe(
    as_of: date,
    *,
    refresh_lists: bool = False,
) -> list[BoardUniverse]:
    """Industry + concept boards with constituents, using the daily member cache.

    Each call is one scan: the per-scan clist request budget restarts here,
    while a ban cooldown (if one tripped earlier) is still honored.

    A bulk all-market sweep runs first when much of the taxonomy is still cold:
    it covers hundreds of boards for a handful of requests, so the scan does not
    spend its whole inline budget filling 40 boards one by one.
    """
    global _scan_started_at
    _reset_clist_state()
    _scan_started_at = time.monotonic()
    boards = _load_board_list(as_of, refresh=refresh_lists)
    try:
        bulk_fill_members(as_of, boards)
    except Exception:  # noqa: BLE001 — bulk is an accelerator, never a blocker
        logger.exception("market-pick bulk member fill failed; using per-board fill")
    out: list[BoardUniverse] = []
    for meta in boards:
        symbols = _load_constituents(as_of, meta["bk"], refresh=refresh_lists)
        out.append(
            BoardUniverse(
                bk=meta["bk"],
                name=meta["name"],
                board_type=meta["board_type"],  # type: ignore[arg-type]
                symbols=symbols,
            )
        )
    return out


def unique_symbols(boards: list[BoardUniverse]) -> list[str]:
    """Dedup member codes so OHLCV is fetched once, not once per board."""
    seen: set[str] = set()
    out: list[str] = []
    for board in boards:
        for code in board.symbols:
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


def _load_board_list(as_of: date, *, refresh: bool) -> list[dict[str, Any]]:
    path = _list_path(as_of)
    if path.is_file() and not refresh:
        cached_rows = _read_board_list_file(path)
        if cached_rows:
            return cached_rows
    rows: list[dict[str, Any]] = []
    failure: Exception | None = None
    for board_type in ("industry", "concept"):
        try:
            rows.extend(_fetch_board_list(board_type))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 — clist banned/RST: degrade to cache
            failure = exc
            logger.warning("market-pick %s board list failed: %s", board_type, exc)
            break  # clist is down; do not hammer the second taxonomy either
    if failure is not None or not rows:
        fallback = _latest_cached_board_list(as_of)
        if fallback:
            logger.warning(
                "market-pick board list fetch failed (%s); using cached board "
                "universe from %s",
                failure or "empty response",
                _fallback_source_date,
            )
            return fallback
        if rows:
            logger.warning(
                "market-pick board list: no usable cache, continuing with the "
                "partial list that did fetch"
            )
            return rows
        raise failure or RuntimeError("market-pick board list: empty and no cache")
    _atomic_write(path, rows)
    return rows


def _load_constituents(
    as_of: date,
    bk: str,
    *,
    refresh: bool,
) -> list[str]:
    """Member codes for ``bk``: cache first, network only on a cold cache.

    ``refresh`` is accepted for call-site symmetry with the board list but never
    forces a member pull. A scan must stay cheap — Eastmoney's WAF bans the IP
    after roughly 80 clist calls in a few minutes, so hundreds of per-board
    requests inside one scan is precisely what breaks it. Stale-but-present
    caches are served as-is and refreshed out of band by
    :func:`warm_member_cache`; only a board with no cache at all is fetched here.
    """
    today = _member_path(as_of, bk)
    if today.is_file():
        codes = _read_member_file(today)
        if codes:
            return codes
    entry = _member_cache_entry(as_of, bk)
    if entry is not None:
        return entry[1]
    if not _clist_ready():
        return []
    global _cold_fetches, _cold_stopped
    if _cold_stopped:
        return []
    if _cold_fetches >= _cold_cap() or _cold_deadline_passed():
        _cold_stopped = True
        logger.info(
            "market-pick inline cold fill stopped (%d boards, %d clist calls, "
            "budget %s); remaining boards will be filled by the background warm-up",
            _cold_fetches,
            _clist_calls,
            "spent" if _cold_deadline_passed() else f"cap {_cold_cap()}",
        )
        return []
    _cold_fetches += 1
    try:
        symbols = _fetch_constituents(bk)
    except ClistUnavailable as exc:
        # Breaker/budget: stop calling out for the rest of the scan. Logged once
        # at trip time, not per board.
        logger.debug("market-pick constituents %s skipped: %s", bk, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — one dead board must not abort the day
        logger.warning("market-pick constituents %s failed: %s", bk, exc)
        return []
    if not symbols:
        return []
    _atomic_write(today, symbols)
    return symbols


def _clist_get(params: dict[str, Any]) -> Any:
    """GET clist under the request-pressure guard.

    Order of checks, cheapest first: an open cooldown fails fast without
    touching the network; the per-scan budget caps total calls; only then does
    a host get attempts. A healthy client retries once and falls over to the
    second host; once any failure is on record (or a cooldown probe is armed)
    the call is one-shot — when the IP is banned, every wasted request can
    extend the ban.

    Raises:
        ClistUnavailable: cooling down after repeated failures, or the scan's
            request budget is spent. Callers degrade to the on-disk cache.
        Exception: the last transport error once every host/attempt failed.
    """
    global _working_clist_url, _clist_calls, _consecutive_failures
    global _tripped_until, _probe_pending, _probe_fails

    if not _clist_ready():
        raise ClistUnavailable(
            "clist cooling down after repeated failures; using cached board data"
        )
    budget = _max_clist_requests()
    if _clist_calls >= budget:
        raise ClistUnavailable(
            f"clist request budget ({budget}) exhausted for this scan; "
            "using cached board data"
        )

    if _probe_pending:
        # Only one caller may spend the probe. A concurrent caller either finds
        # the flag cleared (that probe worked — carry on as healthy traffic) or
        # finds the breaker open again (that probe failed — degrade to cache)
        # instead of firing a second request at a banned endpoint.
        with _probe_lock:
            if _probe_pending:
                if not _clist_ready():
                    raise ClistUnavailable(
                        "clist cooling down after a failed probe; "
                        "using cached board data"
                    )
                return _clist_probe(params)

    urls = list(_CLIST_URLS)
    if _working_clist_url in urls:
        urls = [_working_clist_url] + [url for url in urls if url != _working_clist_url]
    # Healthy: two hosts x two attempts. Already failing: a single shot on the
    # sticky host — retrying a banned endpoint only prolongs the ban.
    healthy = _consecutive_failures == 0
    hosts = urls if healthy else urls[:1]
    tries = 2 if healthy else 1
    last_error: Exception | None = None
    for url in hosts:
        for attempt in range(tries):
            if _clist_calls >= budget:
                break
            _clist_calls += 1
            _gate_wait()
            try:
                payload = get_json(url, params=params)
            except Exception as exc:  # noqa: BLE001 — retry, then try next host
                last_error = exc
                _gate_note(False)
                logger.warning(
                    "market-pick clist %s failed (attempt %d): %s", url, attempt + 1, exc
                )
                if attempt + 1 < tries:
                    time.sleep(_RETRY_BACKOFF_S)
                continue
            _gate_note(True)
            _consecutive_failures = 0
            _working_clist_url = url
            if _probe_fails:
                # The endpoint answers again, so yesterday's failed probes are
                # settled history; give today's budget back.
                _probe_fails = 0
                _save_trip_state()
            return payload

    _consecutive_failures += 1
    if _consecutive_failures >= _TRIP_AFTER_FAILURES:
        _trip_cooldown()
        logger.error(
            "market-pick clist tripped open after %d consecutive failures "
            "(~%d calls this scan); backing off %.0fs (escalation #%d today) "
            "and serving cache",
            _consecutive_failures,
            _clist_calls,
            (_tripped_until or time.monotonic()) - time.monotonic(),
            _trip_count,
        )
        raise ClistUnavailable(
            "clist banned/cooling down; using cached board data"
        ) from last_error
    if _clist_calls >= budget:
        raise ClistUnavailable(
            f"clist request budget ({budget}) exhausted for this scan; "
            "using cached board data"
        ) from last_error
    if last_error is not None:
        raise last_error
    raise RuntimeError("market-pick clist: no Eastmoney host configured")


def _clist_probe(params: dict[str, Any]) -> Any:
    """One-shot probe after a cooldown: success reopens clist, failure backs off.

    The probe spends exactly one request — no retry, no second host — because
    probing a still-banned IP with a full retry grid is what kept re-extending
    the ban. A failed probe doubles the *next* quiet window instead of counting
    as a fresh trip (which would reset the ladder to 15min), and once the day's
    probe budget is spent the endpoint is left alone until midnight.
    """
    global _working_clist_url, _clist_calls, _consecutive_failures
    global _tripped_until, _probe_pending, _probe_fails

    url = _working_clist_url or _CLIST_URLS[0]
    _clist_calls += 1
    _gate_wait()
    try:
        payload = get_json(url, params=params)
    except Exception as exc:  # noqa: BLE001 — a failed probe backs off at once
        _gate_note(False)
        cooldown = _trip_cooldown(probe_failure=True)
        budget = _max_probes_per_day()
        if _probe_fails >= budget:
            cooldown = max(cooldown, _seconds_until_midnight())
            _tripped_until = time.monotonic() + cooldown
            _save_trip_state()
            logger.error(
                "market-pick clist probe failed (%d/%d probes used today); "
                "clist closed for %.0fs and serving cached boards",
                _probe_fails,
                budget,
                cooldown,
            )
        else:
            logger.error(
                "market-pick clist probe failed; backing off %.0fs "
                "(trip #%d, failed probe %d/%d today)",
                cooldown,
                _trip_count,
                _probe_fails,
                budget,
            )
        raise ClistUnavailable(
            "clist still banned after cooldown probe; using cached board data"
        ) from exc
    _gate_note(True)
    _probe_pending = False
    _consecutive_failures = 0
    _working_clist_url = url
    if _probe_fails:
        _probe_fails = 0
        _save_trip_state()
    logger.info("market-pick clist probe succeeded; endpoint reopened")
    return payload


def _as_float(value: Any) -> float:
    """clist numeric field → float, treating the ``-`` placeholder as zero."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _payload_total(payload: Any) -> int:
    """Server-reported row total for a clist payload (0 when absent/unknown)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def _page_count(total: int, pz: int, cap: int) -> int:
    """Pages needed for ``total`` rows, capped — one request saved per board."""
    if total <= 0:
        return cap
    return max(1, min(cap, -(-total // pz)))


def _fetch_board_list(board_type: BoardType) -> list[dict[str, Any]]:
    fs = _FS[board_type]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    fetched = 0
    # pz=1000 pulls the whole taxonomy in one call (clist honors large page
    # sizes; akshare fetches the full A-share list with pz=50000). One request
    # per taxonomy instead of up to seven is the single biggest brake here.
    pages = 2
    pz = 1000
    pn = 1
    while pn <= pages:
        payload = _clist_get(
            {
                "pn": pn,
                "pz": pz,
                "po": 1,
                "np": 1,
                "ut": _UT,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": fs,
                "fields": _LIST_FIELDS,
            },
        )
        diff = _diff_rows(payload)
        if not diff:
            break
        fetched += len(diff)
        for item in diff:
            if not isinstance(item, dict):
                continue
            bk = str(item.get("f12") or "").strip().upper()
            name = str(item.get("f14") or "").strip()
            if not bk or not name or bk in seen:
                continue
            if any(token in name for token in _EXCLUDE_NAME_SUBSTR):
                continue
            seen.add(bk)
            rows.append(
                {
                    "bk": bk,
                    "name": name,
                    "board_type": board_type,
                    "turnover": _as_float(item.get("f6")),
                }
            )
        total = _payload_total(payload)
        if total and fetched >= total:
            break
        # Trust ``total`` for the page budget: excluded/hype boards are filtered
        # out of ``seen``, so counting unique kept rows never reaches it. Page
        # size comes from what the server ACTUALLY returned, so a server that
        # silently caps ``pz`` still pages through the full list instead of
        # truncating the taxonomy.
        pages = _page_count(total, len(diff), 7)
        pn += 1
    return rows


def _fetch_constituents(bk: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    fetched = 0
    # pz=1000: every board fits one call (the largest boards are ~500 names),
    # so a full cold sweep costs exactly one request per board instead of up
    # to six paged calls.
    pages = 2
    pz = 1000
    pn = 1
    while pn <= pages:
        payload = _clist_get(
            {
                "pn": pn,
                "pz": pz,
                "po": 1,
                "np": 1,
                "ut": _UT,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{bk}",
                "fields": _MEMBER_FIELDS,
            },
        )
        diff = _diff_rows(payload)
        if not diff:
            break
        fetched += len(diff)
        for item in diff:
            if not isinstance(item, dict):
                continue
            code = _to_ts_code(item)
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
        total = _payload_total(payload)
        if total and fetched >= total:
            break
        # Same trick as the board list: ``total`` counts B-shares/BJ that the
        # code filter drops, so never page until ``seen`` matches it; page size
        # follows the server's actual row count so a capped ``pz`` cannot
        # silently truncate a board's constituents.
        pages = _page_count(total, len(diff), 6)
        pn += 1
    return codes


def _to_ts_code(row: dict[str, Any]) -> str | None:
    raw = str(row.get("f12") or "").strip().zfill(6)
    if not raw.isdigit():
        return None
    # BJ + B-shares: dual-track tape is A-share only; Tencent 501s on 900/200.
    if raw.startswith(("4", "8", "92", "900", "200")):
        return None
    market = int(row.get("f13") or 0)
    if market == 1:
        return f"{raw}.SH"
    return f"{raw}.SZ"


def _diff_rows(payload: Any) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, dict):
        return list(diff.values())
    if isinstance(diff, list):
        return diff
    return []


# --- Bulk membership sweep ---------------------------------------------------
# Per-board pulls cannot cover the taxonomy: the WAF allows only a few dozen
# clist calls per window while there are ~1000 boards, which is exactly the
# "many boards show no data" symptom. One query over the whole A-share tape
# carries every stock's industry (``f100``) and concept tags (``f103``), so the
# same coverage costs a handful of calls instead of a thousand.


def _bulk_state_path() -> Path:
    return market_pick_dir() / "members" / "_bulk_state.json"


def _read_bulk_state() -> dict[str, Any]:
    try:
        raw = json.loads(_bulk_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_bulk_state(payload: dict[str, Any]) -> None:
    try:
        _atomic_write(_bulk_state_path(), payload)
    except OSError as exc:  # a bookkeeping file must never break a scan
        logger.debug("market-pick bulk state write failed: %s", exc)


def _split_tags(value: Any) -> list[str]:
    """``f103`` may be a list or a delimited string; ``-`` means no tag."""
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = re.split(r"[,，、;；|]", str(value or ""))
    out: list[str] = []
    for item in items:
        name = item.strip()
        if name and name not in {"-", "--", "—", "None", "null"}:
            out.append(name)
    return out


def _bulk_sweep() -> dict[str, dict[str, list[str]]] | None:
    """One pass over the whole A-share tape → ``{board_type: {name: [codes]}}``.

    Returns ``None`` when the sweep did not complete (call cap reached, server
    truncated the pages, breaker tripped). A partial sweep is thrown away
    rather than written: a truncated member list would look valid for the whole
    member TTL and silently mis-rank boards.
    """
    merged: dict[str, dict[str, set[str]]] = {"industry": {}, "concept": {}}
    pz = _BULK_PAGE_SIZE
    max_calls = _bulk_max_calls()
    calls = 0
    fetched = 0
    total = 0
    pn = 1
    while calls < max_calls:
        try:
            payload = _clist_get(
                {
                    "pn": pn,
                    "pz": pz,
                    "po": 1,
                    "np": 1,
                    "ut": _UT,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f12",
                    "fs": _BULK_FS,
                    "fields": _BULK_FIELDS,
                },
            )
        except ClistUnavailable:
            return None
        except Exception as exc:  # noqa: BLE001 — bulk is a bonus, never a blocker
            logger.warning("market-pick bulk sweep failed: %s", exc)
            return None
        calls += 1
        rows = _diff_rows(payload)
        if not rows:
            break
        fetched += len(rows)
        total = _payload_total(payload) or total
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = _to_ts_code(row)
            if not code:
                continue
            for kind, value in (
                ("industry", row.get("f100")),
                ("concept", row.get("f103")),
            ):
                for name in _split_tags(value):
                    merged[kind].setdefault(name, set()).add(code)
        if total and fetched >= total:
            break
        # Page size follows what the server actually returned, so a capped
        # ``pz`` still pages through the tape — it just needs more calls than
        # the cap allows, and an incomplete sweep is thrown away.
        page_cap = max(1, -(-total // len(rows))) if total else max_calls
        if pn >= page_cap:
            break
        pn += 1

    if not total or fetched < total:
        logger.info(
            "market-pick bulk sweep incomplete (%d/%d rows, %d calls); "
            "keeping per-board fill",
            fetched,
            total,
            calls,
        )
        return None
    return {
        kind: {name: sorted(codes) for name, codes in bucket.items() if codes}
        for kind, bucket in merged.items()
    }


def _bulk_plausible(kind: str, mapping: dict[str, list[str]]) -> tuple[bool, str]:
    """Shape check: a real mapping covers most of the tape, not a few names."""
    if len(mapping) < 10:
        return False, f"only {len(mapping)} names matched"
    codes: set[str] = set()
    sizes: list[int] = []
    for bucket in mapping.values():
        codes.update(bucket)
        sizes.append(len(bucket))
    floor = _BULK_MIN_CODES.get(kind, 1500)
    if len(codes) < floor:
        return False, f"{len(codes)} distinct codes < {floor}"
    sizes.sort()
    median = sizes[len(sizes) // 2]
    if not 3 <= median <= 800:
        return False, f"median board size {median} out of range"
    return True, f"{len(mapping)} names / {len(codes)} codes / median {median}"


def _bulk_cross_check(
    kind: str,
    mapping: dict[str, list[str]],
    boards: list[dict[str, Any]],
    as_of: date,
) -> tuple[bool, str]:
    """Compare the sweep with member files the per-board path already wrote.

    This is the guard against a name-based mapping that looks structurally fine
    but is assigned to the wrong level of the taxonomy: if the sweep disagrees
    with data we already trust, it is discarded instead of overwriting it.

    Agreement is judged per board and then by majority, not by the mean: one
    stale or half-filled cache file must not veto an otherwise correct sweep.
    """
    ratios: list[float] = []
    for meta in boards:
        if meta.get("board_type") != kind:
            continue
        codes = mapping.get(str(meta.get("name") or ""))
        if not codes:
            continue
        entry = _member_cache_entry(as_of, str(meta.get("bk") or ""))
        if entry is None or len(entry[1]) < 10:
            continue
        cached = set(entry[1])
        ratios.append(len(cached & set(codes)) / len(cached))
        if len(ratios) >= _BULK_VALIDATE_SAMPLES:
            break
    if len(ratios) < 3:
        return True, f"no trusted sample to cross-check ({len(ratios)})"
    agreeing = sum(1 for ratio in ratios if ratio >= _BULK_VALIDATE_FLOOR)
    median = sorted(ratios)[len(ratios) // 2]
    verdict = f"median overlap {median:.0%}, {agreeing}/{len(ratios)} boards agree"
    return agreeing * 2 >= len(ratios), verdict


def bulk_fill_members(as_of: date, boards: list[dict[str, Any]]) -> int:
    """Fill every missing member cache from one all-market sweep.

    Only missing boards are written; an existing (per-board or earlier bulk)
    file is authoritative and never overwritten. At most one sweep is attempted
    per calendar day — the state file records ``ok``/``rejected`` so repeated
    scans and process restarts do not re-spend the calls.
    """
    if not boards or not _clist_ready():
        return 0
    state = _read_bulk_state()
    if state.get("date") == as_of.isoformat():
        if state.get("status") in {"ok", "rejected"}:
            return 0
        # A sweep that came back truncated would come back truncated again;
        # retrying it on every scan would burn the whole call budget.
        age = time.time() - _as_float(state.get("at"))
        if state.get("status") == "aborted" and age < _BULK_RETRY_AFTER_S:
            return 0
    missing = [
        meta for meta in boards if _member_cache_entry(as_of, str(meta.get("bk") or "")) is None
    ]
    if len(missing) < _bulk_min_missing():
        return 0

    swept = _bulk_sweep()
    if swept is None:
        _write_bulk_state({"date": as_of.isoformat(), "status": "aborted", "at": time.time()})
        return 0

    written = 0
    verdicts: list[str] = []
    for kind in ("industry", "concept"):
        mapping = swept.get(kind) or {}
        kind_boards = [meta for meta in boards if meta.get("board_type") == kind]
        matched = sum(1 for meta in kind_boards if str(meta.get("name") or "") in mapping)
        coverage = matched / len(kind_boards) if kind_boards else 0.0
        ok, why = _bulk_plausible(kind, mapping)
        # Coverage guards against a field that only carries the coarse level:
        # 86 top-level industries would tag a fraction of a 496-board taxonomy.
        if ok and coverage < _BULK_MIN_COVERAGE:
            ok = False
            why = f"only {matched}/{len(kind_boards)} boards tagged"
        if ok:
            ok, why = _bulk_cross_check(kind, mapping, boards, as_of)
        verdicts.append(f"{kind}: {'accepted' if ok else 'rejected'} ({why})")
        if not ok:
            # Visible on purpose: a rejected mapping means bulk coverage is off
            # for the day and only the slow per-board path is filling boards.
            logger.warning(
                "market-pick bulk %s mapping rejected (%s); falling back to per-board fill",
                kind,
                why,
            )
            continue
        for meta in kind_boards:
            bk = str(meta.get("bk") or "")
            codes = mapping.get(str(meta.get("name") or ""))
            if not bk or not codes:
                continue
            path = _member_path(as_of, bk)
            if path.is_file():
                continue
            try:
                _atomic_write(path, codes, durable=False)
            except OSError as exc:
                logger.debug("market-pick bulk write %s failed: %s", bk, exc)
                continue
            written += 1

    _write_bulk_state(
        {
            "date": as_of.isoformat(),
            "status": "ok" if written else "rejected",
            "filled": written,
            "verdicts": verdicts,
            "at": time.time(),
        }
    )
    logger.info(
        "market-pick bulk member sweep: %d boards filled from one all-market pass (%s)",
        written,
        "; ".join(verdicts),
    )
    return written


def _list_path(as_of: date) -> Path:
    return market_pick_dir() / "boards" / f"{as_of.isoformat()}.json"


def _member_path(as_of: date, bk: str) -> Path:
    return market_pick_dir() / "members" / as_of.isoformat() / f"{bk.upper()}.json"


# Date of the cache file the last fallback served (for warning logs).
_fallback_source_date: str | None = None


def _usable_cache_dates(directory: Path, as_of: date) -> list[date]:
    """Cache dates at or before ``as_of`` within the staleness window, newest first."""
    if not directory.is_dir():
        return []
    cutoff = as_of - timedelta(days=_MAX_CACHE_AGE_DAYS)
    dates: list[date] = []
    for child in directory.iterdir():
        if not child.is_file() or child.suffix != ".json":
            continue
        try:
            cached = date.fromisoformat(child.stem)
        except ValueError:
            continue
        if cutoff <= cached <= as_of:
            dates.append(cached)
    dates.sort(reverse=True)
    return dates


def _latest_cached_board_list(as_of: date) -> list[dict[str, Any]] | None:
    """Most recent prior-day board list within the staleness window, if any."""
    global _fallback_source_date
    _fallback_source_date = None
    directory = market_pick_dir() / "boards"
    for cached in _usable_cache_dates(directory, as_of):
        path = directory / f"{cached.isoformat()}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("market-pick board cache %s unreadable: %s", path, exc)
            continue
        if isinstance(raw, list) and raw:
            rows = [row for row in raw if isinstance(row, dict) and row.get("bk")]
            if rows:
                _fallback_source_date = cached.isoformat()
                return rows
    return None


def _read_member_file(path: Path) -> list[str] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("market-pick member cache %s unreadable: %s", path, exc)
        return None
    if not isinstance(raw, list):
        return None
    codes = [str(code).upper() for code in raw if str(code).strip()]
    return codes or None


def _member_cache_entry(as_of: date, bk: str) -> tuple[date, list[str]] | None:
    """Newest cached member list for ``bk`` at or before ``as_of``.

    Returns the cache date with the codes so callers can apply their own TTL:
    a member list is not date-specific data, it only becomes stale after a
    while, and re-pulling all ~490 boards per day is what gets the IP banned.
    """
    root = market_pick_dir() / "members"
    if not root.is_dir():
        return None
    cutoff = as_of - timedelta(days=_MAX_CACHE_AGE_DAYS)
    bk_file = f"{bk.upper()}.json"
    candidates: list[tuple[date, Path]] = []
    for day_dir in root.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            cached = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if not (cutoff <= cached <= as_of):
            continue
        path = day_dir / bk_file
        if path.is_file():
            candidates.append((cached, path))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for cached, path in candidates:
        codes = _read_member_file(path)
        if codes:
            return cached, codes
    return None


def _read_board_list_file(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("market-pick board list cache unreadable: %s", exc)
        return None
    if not isinstance(raw, list):
        return None
    rows = [row for row in raw if isinstance(row, dict) and row.get("bk")]
    return rows or None


def warm_work_pending(as_of: date | None = None) -> bool:
    """Whether a warm-up run has anything to do — cache-only, never network.

    Used as a cheap gate before spawning the background thread, so a fresh
    install (no board-list cache yet) or a fully warm cache starts nothing.
    Falls back to the newest prior board list, because the very case that
    leaves most boards cold — today's taxonomy fetch being blocked — is also
    the case where today's list file does not exist.
    """
    target = as_of or date.today()
    boards = _read_board_list_file(_list_path(target)) or _latest_cached_board_list(target)
    if not boards:
        return False
    missing, expired = _member_refresh_work(target, boards)
    return bool(missing or expired)


def member_coverage(boards: list[BoardUniverse]) -> float:
    """Share of boards with at least one classified constituent (0..1)."""
    if not boards:
        return 1.0
    covered = sum(1 for board in boards if board.symbols)
    return covered / len(boards)


def _member_refresh_work(
    as_of: date,
    boards: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Split boards into ``(missing cache, expired cache)``, oldest expired first.

    Missing boards are unbounded (a cold install needs them all, and the pace +
    breaker keep that safe); expired boards are quota-limited by the caller so a
    mass TTL expiry never becomes one big burst.
    """
    ttl = _member_ttl_days()
    missing: list[tuple[float, str]] = []
    expired: list[tuple[int, str]] = []
    for meta in boards:
        bk = meta["bk"]
        if _member_path(as_of, bk).is_file():
            continue
        entry = _member_cache_entry(as_of, bk)
        if entry is None:
            missing.append((_as_float(meta.get("turnover")), bk))
            continue
        age = (as_of - entry[0]).days
        if age > ttl:
            expired.append((age, bk))
    # Liquid boards first: when the quota only lets a few hundred boards through
    # per day, the ones people actually look at must not wait behind dead ones.
    missing.sort(key=lambda item: item[0], reverse=True)
    expired.sort(reverse=True)
    return [bk for _, bk in missing], [bk for _, bk in expired]


def warm_member_cache(
    as_of: date | None = None,
    *,
    pace_s: float | None = None,
    expire_quota: int | None = None,
    max_waits: int = 4,
) -> dict[str, int]:
    """Fill missing and expired member caches at a pace Eastmoney tolerates.

    The bulk sweep runs first (a handful of calls for hundreds of boards); only
    what it cannot name is filled here, one board at a time. A full per-board
    sweep would be ~1000 calls, far past the WAF's per-IP limit, so the rest
    runs in a slow background thread: one call every ``pace_s`` (default 8s) and
    at most ``expire_quota`` TTL refreshes per run, liquid boards first. When
    the clist breaker trips it waits out the cooldown and resumes
    (``max_waits`` times) instead of giving up.
    """
    target = as_of or date.today()
    pace = _warm_pace_s() if pace_s is None else pace_s
    quota = _warm_expire_quota() if expire_quota is None else expire_quota
    stats = {
        "total": 0,
        "queued": 0,
        "filled": 0,
        "bulk": 0,
        "skipped": 0,
        "failed": 0,
        "deferred": 0,
    }
    boards = _read_board_list_file(_list_path(target)) or _latest_cached_board_list(target)
    if not boards:
        # No taxonomy cached at all: one fetch is unavoidable, and the clist
        # guard still applies to it.
        boards = _load_board_list(target, refresh=False)
    stats["total"] = len(boards)
    # Same bulk accelerator as the scan: hundreds of boards for a few calls
    # before falling back to the slow one-by-one pace.
    try:
        stats["bulk"] = bulk_fill_members(target, boards)
    except Exception:  # noqa: BLE001 — never let the accelerator end the sweep
        logger.exception("market-pick bulk member fill failed during warm-up")
        stats["bulk"] = 0
    missing, expired = _member_refresh_work(target, boards)
    stats["skipped"] = len(boards) - len(missing) - len(expired)
    work = missing + expired[: max(0, quota)]
    stats["queued"] = len(work)

    index = 0
    waits = 0
    while index < len(work):
        bk = work[index]
        try:
            symbols = _fetch_constituents(bk)
        except ClistUnavailable:
            if waits >= max_waits:
                stats["deferred"] = len(work) - index
                logger.warning(
                    "market-pick warm-up paused at %d/%d boards; clist is "
                    "cooling down, remaining work waits for the next run",
                    index,
                    len(work),
                )
                break
            waits += 1
            wait_s = _clist_resume_wait_s()
            logger.warning(
                "market-pick warm-up cooling down (%d/%d done); resuming in %.0fs",
                index,
                len(work),
                wait_s,
            )
            time.sleep(wait_s)
            continue
        except Exception as exc:  # noqa: BLE001 — one board must not end the sweep
            stats["failed"] += 1
            logger.debug("market-pick warm-up %s failed: %s", bk, exc)
            index += 1
            continue
        if symbols:
            _atomic_write(_member_path(target, bk), symbols)
            stats["filled"] += 1
        index += 1
        if pace > 0:
            time.sleep(pace)
    if stats["filled"]:
        logger.info(
            "market-pick warm-up: %d filled, %d skipped, %d failed, %d deferred "
            "(of %d boards)",
            stats["filled"],
            stats["skipped"],
            stats["failed"],
            stats["deferred"],
            stats["total"],
        )
    return stats


def _atomic_write(path: Path, payload: Any, *, durable: bool = True) -> None:
    """Write JSON through a temp file + rename.

    ``durable=False`` skips the fsync: a bulk pass writes hundreds of cache
    files inside one scan, and each fsync costs ~15ms on Windows. A torn file
    after a hard crash is harmless here — the reader treats it as a cold board
    and refills it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".mp_", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
