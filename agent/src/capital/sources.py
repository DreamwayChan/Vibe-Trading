"""External data adapters for the capital-flow monitor.

Every adapter is network-bound and cached on disk under
``~/.vibe-trading/capital/cache/`` so a rebuild only fetches what is missing:

===============  =====================================================  ==========
source           endpoint                                              cache TTL
===============  =====================================================  ==========
SSE ETF shares   query.sse.com.cn (per trading day)                     settled days: forever
SZSE ETF shares  fund.szse.cn (date range, via akshare)                settled days: forever
fund NAV         fund.eastmoney.com/pingzhongdata/<code>.js            6 hours
index close      sina (via akshare)                                     30 minutes
CSI weights      csindex.com.cn (via akshare)                          7 days
constituent mcap push2.eastmoney.com (ulist, 50 codes per call)         6 hours
===============  =====================================================  ==========

Adapters raise :class:`CapitalSourceError` on failure; callers decide whether a
miss is fatal (index / market cap) or degradable (one missing fund).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from src.capital.universe import INDEX_LABEL, INDEX_SINA_SYMBOL

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HTTP_TIMEOUT = 15.0
_RETRIES = 3
_BACKOFF_S = 0.8

# Cache freshness (seconds).
_TTL_RECENT_SHARES = 20 * 60
# An empty share snapshot is a *publish gap*, not a settled fact: the SSE query
# answers 200 with zero rows until the evening update, and the SZSE workbook
# silently omits a day it has not published yet. Such an answer may only be
# reused briefly, and is retried a bounded number of times before being accepted
# as a real gap (a genuine holiday / a day before the fund list existed).
_TTL_EMPTY_SHARES = 20 * 60
_MAX_EMPTY_ATTEMPTS = 8
_TTL_NAV = 6 * 3600
_TTL_INDEX = 30 * 60
_TTL_MCAP = 6 * 3600
_TTL_WEIGHTS = 7 * 24 * 3600

_SSE_SHARES_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_SHARES_SQL = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
_EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_FUND_JS_URL = "http://fund.eastmoney.com/pingzhongdata/{code}.js"

# The SSE feed reports shares in 万份.
_SSE_SHARE_UNIT = 10_000.0


class CapitalSourceError(RuntimeError):
    """Raised when a data source cannot be reached or parsed."""


# ---------------------------------------------------------------------------
# cache helpers
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()


def cache_root() -> Path:
    """``~/.vibe-trading/capital/cache`` (created on demand)."""
    path = Path.home() / ".vibe-trading" / "capital" / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(kind: str, name: str) -> Path:
    folder = cache_root() / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.json"


def _read_cache(path: Path, ttl: float | None) -> Any | None:
    """Return cached payload when it is younger than ``ttl`` seconds."""
    try:
        if not path.exists():
            return None
        if ttl is not None:
            age = time.time() - path.stat().st_mtime
            if age > ttl:
                return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload.get("data") if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, data: Any) -> None:
    """Atomically persist ``data`` (best effort — never fails the request)."""
    try:
        payload = {"saved_at": datetime.now(timezone.utc).isoformat(), "data": data}
        with _cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    except OSError as exc:  # pragma: no cover - disk issues are environmental
        logger.debug("capital cache write failed for %s: %s", path, exc)


def _read_payload(path: Path) -> dict[str, Any] | None:
    """Raw cache envelope (``saved_at`` / ``data`` / ``attempts``)."""
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _cache_age(path: Path) -> float:
    """Seconds since the cache file was written (``inf`` when unreadable)."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return float("inf")


def _load_share_snapshot(path: Path, settled: bool) -> dict[str, float] | None:
    """Per-day share snapshot from cache, or ``None`` when it must be refetched.

    A **non-empty** snapshot is final once the session is settled — the bourse
    never revises it. An **empty** one is not: it means we asked before the
    day's file was published. Freezing that answer forever is what left
    2026-11-03 as a permanent "未披露" column (both bourses answered 200 with
    zero rows on the evening of 11-03; the day then became "settled", so the
    empty file was served for every later rebuild even though the data had been
    online for hours). Empties are therefore re-asked after ``_TTL_EMPTY_SHARES``
    until ``_MAX_EMPTY_ATTEMPTS`` is reached, which heals a lag while still
    refusing to hammer the bourse over a genuine gap.
    """
    payload = _read_payload(path)
    if payload is None:
        return None
    data = payload.get("data")
    if isinstance(data, dict) and data:
        return {str(k): float(v) for k, v in data.items()}

    age = _cache_age(path)
    attempts = int(payload.get("attempts") or 1)
    if not settled:
        # Same-day: always retryable after the short TTL.
        return {} if age <= _TTL_RECENT_SHARES else None
    if attempts >= _MAX_EMPTY_ATTEMPTS or age <= _TTL_EMPTY_SHARES:
        return {}
    return None


def _write_share_snapshot(path: Path, data: dict[str, float], attempts: int = 1) -> None:
    """Persist a share snapshot together with its attempt counter."""
    try:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
            "attempts": int(attempts),
        }
        with _cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    except OSError as exc:  # pragma: no cover - disk issues are environmental
        logger.debug("capital cache write failed for %s: %s", path, exc)


def _get(url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> requests.Response:
    """GET with retries and a browser-ish UA."""
    last: Exception | None = None
    merged = {"User-Agent": _UA}
    if headers:
        merged.update(headers)
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=merged, timeout=_HTTP_TIMEOUT)
            if resp.status_code >= 500:
                raise CapitalSourceError(f"HTTP {resp.status_code} from {url}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - normalised below
            last = exc
            if attempt < _RETRIES - 1:
                time.sleep(_BACKOFF_S * (attempt + 1))
    raise CapitalSourceError(f"{url} unavailable: {last}") from last


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _compact(day: str) -> str:
    return day.replace("-", "")


def _dashed(day: str) -> str:
    day = _compact(day)
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


# ---------------------------------------------------------------------------
# exchange-published ETF shares
# ---------------------------------------------------------------------------


def sse_etf_shares(day: str) -> dict[str, float]:
    """Shares outstanding (份) for every SSE-listed ETF on ``day``.

    Args:
        day: ``YYYY-MM-DD`` or ``YYYYMMDD``. Settled (past) days are cached
            permanently; the newest day is cached for 20 minutes.
    """
    day = _dashed(day)
    path = _cache_path("shares", f"sse_{_compact(day)}")
    settled = day < _today()
    cached = _load_share_snapshot(path, settled)
    if cached is not None:
        return cached

    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
        "sqlId": _SSE_SHARES_SQL,
        "STAT_DATE": day,
    }
    try:
        resp = _get(_SSE_SHARES_URL, params=params, headers={"Referer": "https://www.sse.com.cn/"})
        rows = (resp.json() or {}).get("result") or []
    except (ValueError, AttributeError) as exc:
        raise CapitalSourceError(f"SSE shares parse failed for {day}: {exc}") from exc

    out: dict[str, float] = {}
    for row in rows:
        code = str(row.get("SEC_CODE") or "").strip()
        try:
            shares = float(row.get("TOT_VOL"))
        except (TypeError, ValueError):
            continue
        if not code or not math.isfinite(shares) or shares <= 0:
            continue
        out[code] = shares * _SSE_SHARE_UNIT

    # A successful empty answer (weekend/holiday before the fund list existed)
    # is cached so the window rebuild does not re-ask for it every time — but
    # only with a bounded number of retries, so a same-day publish lag heals
    # instead of freezing the day (see ``_load_share_snapshot``). A failed
    # request never reaches this point.
    previous = _read_payload(path) or {}
    attempts = int(previous.get("attempts") or 0) + 1
    _write_share_snapshot(path, out, attempts)
    return out


def szse_etf_shares(start: str, end: str) -> dict[str, dict[str, float]]:
    """Shares outstanding for SZSE-listed ETFs in ``[start, end]``.

    Returns ``{day: {code: shares}}``. The bourse publishes the range as one
    xlsx workbook, so results are fanned out into the same per-day cache layout
    the SSE adapter uses.
    """
    start, end = _dashed(start), _dashed(end)
    dates = _trading_day_span(start, end)
    if not dates:
        return {}

    resolved: dict[str, dict[str, float]] = {}
    pending: list[str] = []
    for day in dates:
        path = _cache_path("shares", f"szse_{_compact(day)}")
        cached = _load_share_snapshot(path, day < _today())
        if cached is None:
            pending.append(day)
        else:
            resolved[day] = cached
    if not pending:
        return resolved

    try:
        import akshare as ak

        frame = ak.fund_scale_daily_szse(
            start_date=_compact(min(pending)),
            end_date=_compact(max(pending)),
            symbol="ETF",
        )
    except Exception as exc:  # noqa: BLE001 - akshare raises many types
        raise CapitalSourceError(f"SZSE shares unavailable: {exc}") from exc

    parsed_by_day: dict[str, dict[str, float]] = {}
    if frame is not None and not getattr(frame, "empty", True):
        if "日期" not in frame.columns or "基金份额" not in frame.columns:
            raise CapitalSourceError("SZSE shares payload missing expected columns")
        work = frame.copy()
        work["日期"] = work["日期"].astype(str).str.slice(0, 10)
        for day, group in work.groupby("日期"):
            parsed: dict[str, float] = {}
            for code, shares in zip(group["基金代码"], group["基金份额"]):
                try:
                    value = float(shares)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    parsed[str(code).zfill(6)] = value
            parsed_by_day[_dashed(day)] = parsed

    for day in pending:
        data = parsed_by_day.get(day, {})
        resolved[day] = data
        # Empty but successful answers are cached too (holidays inside the span,
        # or a day the workbook has not published yet); the retry counter keeps
        # a publish lag retryable while a genuine gap stops being re-asked.
        path = _cache_path("shares", f"szse_{_compact(day)}")
        previous = _read_payload(path) or {}
        attempts = int(previous.get("attempts") or 0) + 1
        _write_share_snapshot(path, data, attempts)
    return resolved


def prefetch_sse_shares(days: Iterable[str], workers: int = 6) -> dict[str, dict[str, float]]:
    """Fetch SSE shares for many days concurrently.

    Missing days are returned as empty dicts (callers decide whether that is a
    gap in the series or a hard failure).
    """
    unique = sorted({_dashed(d) for d in days})
    out: dict[str, dict[str, float]] = {}
    if not unique:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = {pool.submit(sse_etf_shares, day): day for day in unique}
        for fut in as_completed(futures):
            day = futures[fut]
            try:
                out[day] = fut.result()
            except CapitalSourceError as exc:
                logger.warning("SSE shares missing for %s: %s", day, exc)
                out[day] = {}
    return out


# ---------------------------------------------------------------------------
# fund NAV
# ---------------------------------------------------------------------------

_JS_NAME_RE = re.compile(r'fS_name\s*=\s*"([^"]*)"')
_JS_NAV_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", re.S)


def fund_nav_history(code: str) -> dict[str, float]:
    """Daily unit NAV (单位净值) for one fund, keyed by ``YYYY-MM-DD``.

    Source: 天天基金 ``pingzhongdata`` JS bundle, which carries the full NAV
    history in one request. A fund that never published NAV yields ``{}``.
    """
    path = _cache_path("nav", str(code))
    cached = _read_cache(path, _TTL_NAV)
    if isinstance(cached, dict) and cached.get("series"):
        return {str(k): float(v) for k, v in cached["series"].items()}

    try:
        resp = _get(_FUND_JS_URL.format(code=code))
    except CapitalSourceError:
        if isinstance(cached, dict) and cached.get("series"):
            # Serve a stale series rather than dropping the fund entirely.
            return {str(k): float(v) for k, v in cached["series"].items()}
        raise

    text = resp.text
    match = _JS_NAV_RE.search(text)
    if not match:
        raise CapitalSourceError(f"no NAV series for fund {code}")

    try:
        rows = json.loads(match.group(1))
    except ValueError as exc:
        raise CapitalSourceError(f"NAV series for {code} is not valid JSON: {exc}") from exc

    series: dict[str, float] = {}
    for item in rows:
        try:
            stamp = int(item["x"]) / 1000.0
            value = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        series[datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")] = value

    name_match = _JS_NAME_RE.search(text)
    payload = {
        "code": str(code),
        "name": name_match.group(1) if name_match else "",
        "series": series,
    }
    if series:
        _write_cache(path, payload)
    return series


def fund_name(code: str) -> str:
    """Full fund name (empty string when unknown)."""
    cached = _read_cache(_cache_path("nav", str(code)), None)
    if isinstance(cached, dict) and cached.get("name"):
        return str(cached["name"])
    return ""


def prefetch_nav(codes: Iterable[str], workers: int = 6) -> dict[str, dict[str, float]]:
    """NAV history for many funds, concurrently."""
    out: dict[str, dict[str, float]] = {}
    unique = [str(c) for c in dict.fromkeys(codes)]
    if not unique:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
        futures = {pool.submit(fund_nav_history, code): code for code in unique}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                out[code] = fut.result()
            except CapitalSourceError as exc:
                logger.warning("NAV missing for %s: %s", code, exc)
                out[code] = {}
    return out


# ---------------------------------------------------------------------------
# index level
# ---------------------------------------------------------------------------


def index_close_history(symbol: str = INDEX_SINA_SYMBOL) -> dict[str, float]:
    """Daily close of the benchmark index, keyed by ``YYYY-MM-DD``."""
    path = _cache_path("index", _compact(symbol if symbol.isalnum() else "index"))
    cached = _read_cache(path, _TTL_INDEX)
    if isinstance(cached, dict) and cached:
        return {str(k): float(v) for k, v in cached.items()}

    try:
        import akshare as ak

        frame = ak.stock_zh_index_daily(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        if isinstance(cached, dict) and cached:
            return {str(k): float(v) for k, v in cached.items()}
        raise CapitalSourceError(f"index history unavailable for {symbol}: {exc}") from exc

    if frame is None or getattr(frame, "empty", True):
        raise CapitalSourceError(f"index history empty for {symbol}")

    series: dict[str, float] = {}
    for day, close in zip(frame["date"], frame["close"]):
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            series[str(day)[:10]] = value
    if not series:
        raise CapitalSourceError(f"index history unparseable for {symbol}")
    _write_cache(path, series)
    return series


# ---------------------------------------------------------------------------
# CSI 300 free-float market cap
# ---------------------------------------------------------------------------


def csi_weights(index_code: str = "000300") -> tuple[str, dict[str, float]]:
    """Official constituent weights (percent) and the weight-file date."""
    path = _cache_path("weights", f"csindex_{index_code}")
    cached = _read_cache(path, _TTL_WEIGHTS)
    if isinstance(cached, dict) and cached.get("weights"):
        return str(cached.get("date") or ""), {str(k): float(v) for k, v in cached["weights"].items()}

    try:
        import akshare as ak

        frame = ak.index_stock_cons_weight_csindex(symbol=index_code)
    except Exception as exc:  # noqa: BLE001
        if isinstance(cached, dict) and cached.get("weights"):
            return str(cached.get("date") or ""), {str(k): float(v) for k, v in cached["weights"].items()}
        raise CapitalSourceError(f"CSI weights unavailable: {exc}") from exc

    if frame is None or getattr(frame, "empty", True):
        raise CapitalSourceError("CSI weights payload is empty")

    weights: dict[str, float] = {}
    weights_date = ""
    for code, weight, as_of in zip(frame["成分券代码"], frame["权重"], frame["日期"]):
        try:
            value = float(weight)
        except (TypeError, ValueError):
            continue
        code = str(code).zfill(6)
        if math.isfinite(value) and value > 0:
            weights[code] = value
    if not weights:
        raise CapitalSourceError("CSI weights payload has no usable rows")
    try:
        weights_date = str(frame["日期"].iloc[0])[:10]
    except Exception:  # noqa: BLE001 - date column shape varies by provider
        weights_date = ""

    _write_cache(path, {"date": weights_date, "weights": weights})
    return weights_date, weights


def _em_secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def constituent_market_caps(codes: Iterable[str]) -> dict[str, dict[str, float]]:
    """Live ``{code: {"total": 总市值, "float": 流通市值}}`` from Eastmoney.

    The endpoints move around between CDN hosts (``push2`` / ``80.push2`` /
    ``push2delay``), so a couple of hosts are tried in order.
    """
    unique = [str(c).zfill(6) for c in dict.fromkeys(codes)]
    out: dict[str, dict[str, float]] = {}
    hosts = ("https://push2.eastmoney.com", "https://push2delay.eastmoney.com")
    for start in range(0, len(unique), 50):
        chunk = unique[start : start + 50]
        params = {
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14,f20,f21",
            "secids": ",".join(_em_secid(c) for c in chunk),
            "ut": _EM_UT,
        }
        rows: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for host in hosts:
            try:
                resp = _get(f"{host}/api/qt/ulist.np/get", params=params, headers={"Referer": "https://quote.eastmoney.com/"})
                rows = ((resp.json() or {}).get("data") or {}).get("diff") or []
                if rows:
                    break
            except (CapitalSourceError, ValueError) as exc:
                last_error = exc
                continue
        if not rows:
            raise CapitalSourceError(f"constituent market caps unavailable: {last_error}")
        for row in rows:
            code = str(row.get("f12") or "").zfill(6)
            total = _num(row.get("f20"))
            free = _num(row.get("f21"))
            if code and (total or free):
                out[code] = {"total": total or 0.0, "float": free or total or 0.0}
    return out


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def csi300_market_cap(index_close: float) -> dict[str, Any]:
    """Estimate the CSI 300 free-float market cap and its per-index-point value.

    Two measurements are taken from the same constituent snapshot:

    * ``circ_mv`` — Σ 流通市值 of all 300 constituents. This is the direct,
      user-facing definition ("ETF 规模 / 沪深300 流通市值").
    * ``weight_implied`` — median of ``流通市值_i / 权重_i``. Because the
      official weights are free-float weights, dividing any constituent's
      market cap by its weight recovers the index's total adjusted market cap;
      the median is robust to individual stocks whose 流通市值 overstates the
      CSI 自由流通市值 (large controlling shareholders).
    """
    path = _cache_path("mcap", "csi300")
    cached = _read_cache(path, _TTL_MCAP)
    if isinstance(cached, dict) and cached.get("total_circ_mv") and index_close > 0:
        cached = dict(cached)
        cached["per_point"] = float(cached["total"]) / index_close if cached.get("total") else None
        return cached

    weights_date, weights = csi_weights()
    caps = constituent_market_caps(weights.keys())

    sum_circ = 0.0
    implied: list[float] = []
    covered = 0
    for code, weight in weights.items():
        cap = caps.get(code)
        if not cap:
            continue
        covered += 1
        sum_circ += cap["float"]
        if weight > 0:
            implied.append(cap["float"] / (weight / 100.0))
    if not covered:
        raise CapitalSourceError("no constituent market caps matched the CSI 300 weights")

    implied.sort()
    implied_median = implied[len(implied) // 2] if implied else 0.0

    payload = {
        "weights_date": weights_date,
        "constituents": len(weights),
        "covered": covered,
        "total_circ_mv": sum_circ,
        "total_weight_implied": implied_median,
        "total": sum_circ,
        "as_of": _today(),
    }
    _write_cache(path, payload)
    payload = dict(payload)
    payload["per_point"] = payload["total"] / index_close if index_close > 0 else None
    return payload


def trading_days(index_series: dict[str, float], window: int) -> list[str]:
    """Last ``window`` trading days present in the index series (ascending)."""
    days = sorted(index_series)
    if window > 0:
        days = days[-(window + 1) :]
    return days


def _trading_day_span(start: str, end: str) -> list[str]:
    """Exchange sessions in a span.

    Delegates to :mod:`src.data.calendar`, which is seeded from the bourse's
    published trade dates and cached under the local lake.  The previous
    implementation only skipped weekends, so every exchange holiday (Spring
    Festival, National Day, Qingming, ...) was treated as a session: the spans
    requested non-existent publications, and any window/percentage built on
    them was off by up to ~15 sessions a year.

    Falls back to the weekday span when the calendar seed is unavailable (fully
    offline dev), which reproduces the old behaviour rather than returning
    nothing.
    """
    try:
        from src.data.calendar import has_calendar, trading_days

        if has_calendar():
            # An empty span is a valid answer here (a whole closed week such as
            # National Day), so branch on the calendar being seeded rather than
            # on the result being non-empty.
            return trading_days(start, end)
    except Exception as exc:  # noqa: BLE001 — calendar is an optimisation, not a gate
        logger.debug("trading calendar unavailable, using weekday span: %s", exc)

    try:
        cur = datetime.strptime(start, "%Y-%m-%d").date()
        last = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return []
    out: list[str] = []
    while cur <= last:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


def today_str() -> str:
    return _today()


def clamp_window(window: int, low: int = 20, high: int = 500) -> int:
    """Bound a caller-supplied window so a typo cannot trigger 5k requests."""
    try:
        value = int(window)
    except (TypeError, ValueError):
        value = 120
    return max(low, min(high, value))


def describe_sources() -> list[dict[str, str]]:
    """Data-source legend rendered in the methodology panel."""
    return [
        {"name": "上交所 ETF 份额", "url": "https://www.sse.com.cn/assortment/fund/etf/list/scale/"},
        {"name": "深交所 ETF 份额", "url": "https://fund.szse.cn/marketdata/fundslist/index.html"},
        {"name": "基金单位净值", "url": "https://fund.eastmoney.com/"},
        {"name": "中证指数成分权重", "url": "https://www.csindex.com.cn/"},
        {"name": "成分股流通市值", "url": "https://quote.eastmoney.com/"},
        {"name": INDEX_LABEL + "指数点位", "url": "https://finance.sina.com.cn/"},
    ]
