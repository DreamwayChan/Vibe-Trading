"""Same-day provisional daily bars for monitor scans.

Daily free loaders often stop at T-1 during the session, or already include a
same-day bar frozen at the open print. For continuous monitoring we append or
refresh today's bar from a live quote so each rescan recalculates in realtime.
Provisional only — official EOD bars may flip the signal after the close.

Reliability notes
-----------------
Domestic quote hosts are often broken by a misconfigured system HTTP(S)_PROXY.
All live-quote HTTP here uses a dedicated session with ``trust_env=False`` so
history loaders can still honour proxies while monitor quotes go direct.

Provider order for A-shares is health-ranked among Eastmoney / Tencent /
Sina (see ``backtest.loaders.source_health``): recently reachable hosts are
tried first, and a short circuit-breaker skips hosts that keep timing out so
one bad path does not stall every symbol in a large universe.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
import requests

from backtest.loaders.source_health import (
    is_cooling_down,
    prefer_order,
    record_failure,
    record_success,
    reset_for_tests as _reset_source_health,
)

logger = logging.getLogger(__name__)

_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
_EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# last, high, low, open, volume, amount, code, name, prev_close
_EASTMONEY_FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f60"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Reuse quotes across same-day rescans / overlapping boards for a short window.
_CACHE_TTL_S = 45.0
# Kept for tests — mirrors source_health's trip threshold.
_EASTMONEY_FAIL_THRESHOLD = 3
# Tencent / Sina accept comma-joined lists; keep batches modest for URL length.
_BATCH_SIZE = 60
# Live-quote providers; order is reshuffled by host health each call.
_A_SHARE_QUOTE_PROVIDERS = ("eastmoney", "tencent", "sina")


@dataclass(frozen=True)
class LiveQuote:
    """Minimal realtime quote used to synthesize today's daily bar."""

    last: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    as_of: str | None = None
    source: str | None = None


_CACHE_LOCK = threading.Lock()
_QUOTE_CACHE: dict[str, tuple[float, LiveQuote]] = {}

_SESSION_LOCK = threading.Lock()
_DIRECT_SESSION: requests.Session | None = None


def fetch_live_quote(symbol: str) -> LiveQuote | None:
    """Fetch a live last price for *symbol* (fail-soft, no broker required)."""
    symbol = _norm_symbol(symbol)
    if not symbol:
        return None
    return fetch_live_quotes([symbol]).get(symbol)


def fetch_live_quotes(symbols: list[str]) -> dict[str, LiveQuote | None]:
    """Fetch live quotes for many symbols with cache, breaker, and batch fallbacks.

    Returns a dict keyed by normalized symbol. Missing / failed symbols map to
    ``None`` so callers can compute coverage without a second pass.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = _norm_symbol(raw)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)

    out: dict[str, LiveQuote | None] = {s: None for s in ordered}
    if not ordered:
        return out

    pending: list[str] = []
    for symbol in ordered:
        cached = _cache_get(symbol)
        if cached is not None:
            out[symbol] = cached
        else:
            pending.append(symbol)

    if not pending:
        return out

    # Prefer recently-reachable hosts so a dead Eastmoney path does not burn
    # a serial timeout on every symbol before Tencent/Sina get a chance.
    for provider in prefer_order(_A_SHARE_QUOTE_PROVIDERS):
        if not pending:
            break
        if provider == "eastmoney":
            pending = _fill_from_eastmoney(pending, out)
        elif provider == "tencent":
            pending = _fill_from_tencent(pending, out)
        elif provider == "sina":
            pending = _fill_from_sina(pending, out)

    return out


def _fill_from_eastmoney(
    pending: list[str], out: dict[str, LiveQuote | None]
) -> list[str]:
    if _eastmoney_is_open():
        logger.info(
            "eastmoney live quote circuit open; skipping %d symbol(s)",
            len(pending),
        )
        return pending
    still: list[str] = []
    for symbol in pending:
        quote = _quote_eastmoney(symbol)
        if quote is not None:
            out[symbol] = quote
            _cache_put(symbol, quote)
        else:
            still.append(symbol)
    return still


def _fill_from_tencent(
    pending: list[str], out: dict[str, LiveQuote | None]
) -> list[str]:
    a_share_pending = [s for s in pending if _is_a_share(s)]
    other = [s for s in pending if not _is_a_share(s)]
    if not a_share_pending:
        return pending
    if is_cooling_down("tencent"):
        return pending
    hits = _quote_tencent_batch(a_share_pending)
    if hits:
        record_success("tencent")
    for symbol, quote in hits.items():
        out[symbol] = quote
        _cache_put(symbol, quote)
    still_a = [s for s in a_share_pending if out[s] is None]
    return still_a + other


def _fill_from_sina(
    pending: list[str], out: dict[str, LiveQuote | None]
) -> list[str]:
    a_share_pending = [s for s in pending if _is_a_share(s)]
    other = [s for s in pending if not _is_a_share(s)]
    if not a_share_pending:
        return pending
    if is_cooling_down("sina"):
        return pending
    hits = _quote_sina_batch(a_share_pending)
    if hits:
        record_success("sina")
    for symbol, quote in hits.items():
        out[symbol] = quote
        _cache_put(symbol, quote)
    still_a = [s for s in a_share_pending if out[s] is None]
    return still_a + other


def ensure_same_day_bar(
    frame: pd.DataFrame,
    quote: LiveQuote | None,
    as_of_date: date,
) -> tuple[pd.DataFrame, bool]:
    """Attach / refresh a provisional *as_of_date* bar from a live quote.

    Daily free loaders often stop at T-1, or already include a same-day bar
    frozen at the open print. Same-day monitor rescans must recalculate from
    the latest quote each time — so when today's bar is already present we
    overwrite its OHLC rather than leaving the morning print stuck.

    Returns:
        ``(frame, provisional)`` — *provisional* is True when today's bar was
        appended or refreshed from *quote*. Missing quote leaves the frame
        unchanged with provisional=False.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame, False

    last_bar_date = _last_bar_date(frame)
    if last_bar_date is None:
        return frame, False
    # Future bars should not be rewritten by today's live quote.
    if last_bar_date > as_of_date:
        return frame, False
    if quote is None or quote.last <= 0:
        return frame, False

    open_px = float(quote.open) if quote.open and quote.open > 0 else float(quote.last)
    high_px = float(quote.high) if quote.high and quote.high > 0 else float(quote.last)
    low_px = float(quote.low) if quote.low and quote.low > 0 else float(quote.last)
    close_px = float(quote.last)
    # Keep OHLC coherent if the feed is partial.
    high_px = max(high_px, open_px, close_px)
    low_px = min(low_px, open_px, close_px)
    volume = float(quote.volume) if quote.volume is not None and quote.volume >= 0 else 0.0

    row = {
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": volume,
    }

    out = frame.copy()
    # Normalize index to midnight timestamps so SignalEngine joins cleanly.
    ts = pd.Timestamp(as_of_date)
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)

    if last_bar_date == as_of_date:
        # Vendor already published today's daily bar (or a prior overlay).
        # Refresh OHLC from the live quote so mid-session rescans are realtime
        # instead of freezing the open print that first landed in the frame.
        last_idx = out.index[-1]
        for col in ("open", "high", "low", "close", "volume"):
            if col in out.columns:
                out.at[last_idx, col] = row[col]
        return out, True

    # Reindex (not loc[new]=dict) so extra columns such as amount stay the
    # existing float dtype. loc-assigning pd.NA triggers a pandas concat
    # FutureWarning on all-NA columns.
    if ts not in out.index:
        extended = out.index.append(pd.DatetimeIndex([ts], name=out.index.name))
        out = out.reindex(extended)
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out.at[ts, col] = row[col]
    out = out.sort_index()
    return out, True


def reset_live_quote_state_for_tests() -> None:
    """Clear cache + shared source-health breakers (test helper only)."""
    with _CACHE_LOCK:
        _QUOTE_CACHE.clear()
    _reset_source_health()


def _norm_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _is_a_share(symbol: str) -> bool:
    return symbol.endswith((".SZ", ".SH", ".BJ"))


def _last_bar_date(frame: pd.DataFrame) -> date | None:
    try:
        ts = pd.Timestamp(frame.index[-1])
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def _direct_session() -> requests.Session:
    """Session that ignores system proxy env (common cause of Eastmoney failures)."""
    global _DIRECT_SESSION
    with _SESSION_LOCK:
        if _DIRECT_SESSION is None:
            session = requests.Session()
            session.trust_env = False
            session.proxies = {}
            session.headers.update({"User-Agent": _DEFAULT_UA})
            _DIRECT_SESSION = session
        return _DIRECT_SESSION


def _cache_get(symbol: str) -> LiveQuote | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _QUOTE_CACHE.get(symbol)
        if hit is None:
            return None
        stored_at, quote = hit
        if now - stored_at > _CACHE_TTL_S:
            _QUOTE_CACHE.pop(symbol, None)
            return None
        return quote


def _cache_put(symbol: str, quote: LiveQuote) -> None:
    with _CACHE_LOCK:
        _QUOTE_CACHE[symbol] = (time.monotonic(), quote)


def _eastmoney_is_open() -> bool:
    """True while the Eastmoney transport circuit is open (shared health)."""
    return is_cooling_down("eastmoney")


def _eastmoney_record_success() -> None:
    record_success("eastmoney")


def _eastmoney_record_failure(exc: BaseException) -> None:
    record_failure("eastmoney", exc)


def _quote_eastmoney(symbol: str) -> LiveQuote | None:
    if _eastmoney_is_open():
        return None
    try:
        from backtest.loaders.eastmoney_client import resolve_secid
    except Exception as exc:  # noqa: BLE001
        logger.debug("eastmoney quote import failed: %s", exc)
        return None

    secid = resolve_secid(symbol)
    if not secid:
        return None
    try:
        resp = _direct_session().get(
            _EASTMONEY_QUOTE_URL,
            params={
                "secid": secid,
                "fields": _EASTMONEY_FIELDS,
                "fltt": 2,  # prices already in yuan
            },
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        _eastmoney_record_failure(exc)
        # One warning per failure is noisy on large universes; breaker logs summary.
        logger.debug("eastmoney live quote failed for %s: %s", symbol, exc)
        return None

    _eastmoney_record_success()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None

    last = _positive_float(data.get("f43"))
    if last is None:
        return None
    return LiveQuote(
        last=last,
        open=_positive_float(data.get("f46")),
        high=_positive_float(data.get("f44")),
        low=_positive_float(data.get("f45")),
        volume=_nonneg_float(data.get("f47")),
        as_of=date.today().isoformat(),
        source="eastmoney",
    )


def _a_share_vendor_code(symbol: str, *, vendor: str) -> str | None:
    parts = symbol.split(".")
    if len(parts) != 2:
        return None
    code, suffix = parts[0], parts[1]
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
    if not prefix:
        return None
    if vendor == "tencent":
        return f"{prefix}{code}"
    if vendor == "sina":
        return f"{prefix}{code}"
    return None


def _chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def _quote_tencent_batch(symbols: list[str]) -> dict[str, LiveQuote]:
    """Batch-fetch A-share quotes from Tencent ``qt.gtimg.cn``."""
    code_to_symbol: dict[str, str] = {}
    for symbol in symbols:
        t_code = _a_share_vendor_code(symbol, vendor="tencent")
        if t_code:
            code_to_symbol[t_code] = symbol

    hits: dict[str, LiveQuote] = {}
    if not code_to_symbol:
        return hits

    for batch in _chunks(list(code_to_symbol.keys()), _BATCH_SIZE):
        url = _TENCENT_QUOTE_URL.format(codes=",".join(batch))
        try:
            resp = _direct_session().get(url, timeout=10)
            text = resp.content.decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            record_failure("tencent", exc)
            logger.warning("tencent live quote batch failed (%d codes): %s", len(batch), exc)
            continue

        # Each line looks like: v_sz300607="51~name~code~last~..."
        for match in re.finditer(r"v_([a-z]{2}\d+)=\"([^\"]*)\"", text):
            t_code = match.group(1)
            symbol = code_to_symbol.get(t_code)
            if not symbol:
                continue
            fields = match.group(2).split("~")
            # [1]=name [2]=code [3]=last [4]=prev [5]=open [6]=volume(手)
            # [33]=high [34]=low
            if len(fields) < 6:
                continue
            last = _positive_float(fields[3])
            if last is None:
                continue
            hits[symbol] = LiveQuote(
                last=last,
                open=_positive_float(fields[5]) if len(fields) > 5 else None,
                high=_positive_float(fields[33]) if len(fields) > 33 else None,
                low=_positive_float(fields[34]) if len(fields) > 34 else None,
                volume=_nonneg_float(fields[6]) if len(fields) > 6 else None,
                as_of=date.today().isoformat(),
                source="tencent",
            )
    return hits


def _quote_sina_batch(symbols: list[str]) -> dict[str, LiveQuote]:
    """Batch-fetch A-share quotes from Sina ``hq.sinajs.cn`` as last-resort."""
    code_to_symbol: dict[str, str] = {}
    for symbol in symbols:
        s_code = _a_share_vendor_code(symbol, vendor="sina")
        if s_code:
            code_to_symbol[s_code] = symbol

    hits: dict[str, LiveQuote] = {}
    if not code_to_symbol:
        return hits

    for batch in _chunks(list(code_to_symbol.keys()), _BATCH_SIZE):
        url = _SINA_QUOTE_URL.format(codes=",".join(batch))
        try:
            # Sina often requires a finance referer; without it the body is empty.
            resp = _direct_session().get(
                url,
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=10,
            )
            text = resp.content.decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            record_failure("sina", exc)
            logger.warning("sina live quote batch failed (%d codes): %s", len(batch), exc)
            continue

        # hq_str_sz002262="name,open,prev,last,high,low,..."
        for match in re.finditer(r"hq_str_([a-z]{2}\d+)=\"([^\"]*)\"", text):
            s_code = match.group(1)
            symbol = code_to_symbol.get(s_code)
            if not symbol:
                continue
            fields = match.group(2).split(",")
            if len(fields) < 6:
                continue
            last = _positive_float(fields[3])
            if last is None:
                continue
            hits[symbol] = LiveQuote(
                last=last,
                open=_positive_float(fields[1]),
                high=_positive_float(fields[4]),
                low=_positive_float(fields[5]),
                volume=_nonneg_float(fields[8]) if len(fields) > 8 else None,
                as_of=date.today().isoformat(),
                source="sina",
            )
    return hits


def _positive_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num <= 0:  # NaN or non-positive
        return None
    return num


def _nonneg_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num < 0:
        return None
    return num
