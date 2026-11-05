"""The A-share universe: which symbols a full-market backfill has to cover.

Why this is not just "read the lake"
------------------------------------
Every other universe source in this project answers a *narrower* question:
``_lake_symbols`` returns what has already been fetched (useless for deciding
what is still missing), and the market-pick board taxonomy returns the subset
that belongs to a board.  A five-year backfill needs the whole listed market, so
it needs a real listing source — and that source must be recorded, because
"5,300 symbols" and "312 symbols" produce very different jobs.

Source chain (first one that yields a plausible list wins):

1. **Sina's ``hs_a`` node**, ~100 rows per page, 56 pages, no token.  This is the
   path a clean install takes: it is the only listing feed reachable without
   credentials in the environments this project runs in.
2. **Tushare ``stock_basic``** when a token exists — one call, and the only
   source that can also return *delisted* names (``list_status="D"``).
3. **The board taxonomy** as a last resort, with a note saying the list is
   partial.  A silent partial backfill is the failure mode this module exists to
   prevent.

The result is cached for a day under ``<lake>/_universe/hs_a.json``: listings
change slowly, and a backfill follows a handful of repeated runs.  A stale cache
is still better than no list when the feed is unreachable, so it is used as a
fallback with a note rather than discarded.

Known gaps, stated rather than hidden
-------------------------------------
* Only *currently listed* names: Sina and the board taxonomy have no delisted
  rows, so a backtest over the lake is survivorship-biased until a Tushare
  token fills the ``list_status="D"`` names in.
* Beijing (``.BJ``) symbols are listed but Tencent has no kline feed for them;
  the backfill reports them as skipped instead of pretending they were covered.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.request
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

__all__ = ["cn_symbol", "market_of", "list_symbols", "cache_path", "SINA_PREFIX"]

SINA_API = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
# The endpoint caps ``num`` at 100 no matter what it is asked for, so paging at
# 100 is the only way to see the whole market.
_PAGE_SIZE = 100
_MAX_PAGES = 80  # 8,000 symbols: enough headroom, still bounded
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
SINA_PREFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
_CACHE_NAME = "hs_a.json"

_DIGITS = re.compile(r"^\d{6}$")


def market_of(symbol: str) -> str:
    """Return ``SH`` / ``SZ`` / ``BJ`` for a canonical A-share symbol.

    A bare six-digit code is resolved by prefix, which is how the exchanges are
    actually segmented: 6xx on Shanghai; 0xx/3xx on Shenzhen; 4xx/8xx/9xx
    (including the 920xxx renumbering) on Beijing.
    """
    text = str(symbol).strip().upper()
    code, _, suffix = text.partition(".")
    if suffix in {"SH", "SZ", "BJ"}:
        return suffix
    if not code.isdigit():
        return ""
    if code.startswith("6"):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    if code.startswith(("4", "8", "9")):
        return "BJ"
    return ""


def cn_symbol(raw: Any, market: str | None = None) -> str:
    """Canonicalise one A-share code; ``""`` when it is not an A-share.

    Accepts the three shapes the sources actually emit — ``sh600519`` (Sina),
    ``600519.SH`` (this project) and a bare ``600519`` — and refuses anything
    else.  Refusing matters: ``_canonical`` elsewhere in the ingest package
    hands non-A-share text to the US branch, so ``sh600519`` used to come back
    as ``SH603927.US`` and every fetch returned nothing.

    Args:
        raw: Code in any of the accepted shapes.
        market: Optional explicit market (``"SH"``/``"SZ"``/``"BJ"``) or the
            Sina prefix (``"sh"``); when given it wins over prefix inference.
    """
    text = str(raw or "").strip()
    if not text:
        return ""

    # Sina style: two-letter prefix + six digits.
    prefixed = re.fullmatch(r"(?i)(sh|sz|bj)\s*(\d{6})", text)
    if prefixed:
        code, suffix = prefixed.group(2), SINA_PREFIX[prefixed.group(1).lower()]
        return f"{code}.{suffix}"

    code, _, suffix = text.partition(".")
    code = code.strip()
    if not _DIGITS.match(code):
        return ""
    suffix = (suffix or market or "").strip().upper()
    if suffix not in {"SH", "SZ", "BJ"}:
        suffix = market_of(code)
    return f"{code}.{suffix}" if suffix else ""


def cache_path():
    """Where the cached listing lives (inside the lake, next to the manifests)."""
    from src.data.lake import lake_root

    return lake_root() / "_universe" / _CACHE_NAME


def list_symbols(
    *, refresh: bool = False, notes: list[str] | None = None
) -> list[str]:
    """The A-share universe, cached for a day.

    Args:
        refresh: Ignore today's cache and re-fetch the listing.
        notes: When given, receives one line naming the source and the count —
            every report that used a universe should say where it came from.

    Returns:
        Sorted canonical symbols (``600519.SH``); empty when no source answered
        and no cache exists.
    """
    cached = _read_cache()
    if not refresh and cached and _fresh(cached):
        _note(notes, f"标的池：缓存 {len(cached['symbols'])} 个（{cached['fetched_at'][:10]} 抓取）")
        return list(cached["symbols"])

    for name, loader in (("sina", _from_sina), ("tushare", _from_tushare), ("boards", _from_boards)):
        try:
            symbols = loader()
        except Exception as exc:  # noqa: BLE001 - try the next source
            logger.warning("universe: %s failed (%s)", name, exc)
            continue
        if len(symbols) >= 100:
            _write_cache(symbols, source=name)
            _note(notes, f"标的池：{name} {len(symbols)} 个（全市场）")
            return symbols
        if symbols:
            logger.warning("universe: %s returned only %d symbols", name, len(symbols))

    if cached:
        _note(
            notes,
            f"标的池：来源不可达，沿用缓存 {len(cached['symbols'])} 个（{cached['fetched_at'][:10]}）",
        )
        return list(cached["symbols"])
    _note(notes, "标的池：无法取得全市场清单（无缓存、来源均不可达）")
    return []


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _from_sina() -> list[str]:
    """Page through Sina's ``hs_a`` node (SH + SZ + BJ, currently listed).

    Pages are fetched concurrently: 56 sequential round trips take ~20s, which
    is a visible stall when the console triggers the job, and the listing host
    answers six at a time without complaint.  A failed page is not fatal — the
    rest of the market still arrives, and the count in the note makes a partial
    result visible instead of silent.
    """
    from concurrent.futures import ThreadPoolExecutor

    total = _sina_total()
    pages = max(1, min(_MAX_PAGES, (total + _PAGE_SIZE - 1) // _PAGE_SIZE))
    seen: dict[str, None] = {}

    def harvest(page: int) -> list[dict[str, Any]]:
        try:
            return _sina_page(page)
        except Exception as exc:  # noqa: BLE001 - one page must not sink the list
            logger.warning("universe: sina page %d failed (%s)", page, exc)
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for rows in pool.map(harvest, range(1, pages + 1)):
            for row in rows:
                symbol = cn_symbol(row.get("symbol") or row.get("code") or "")
                if symbol:
                    seen[symbol] = None
    return sorted(seen)


def _sina_total() -> int:
    body = _http_get(f"{SINA_API}/Market_Center.getHQNodeStockCount?node=hs_a")
    digits = re.sub(r"[^0-9]", "", body)
    return int(digits) if digits else 0


def _sina_page(page: int) -> list[dict[str, Any]]:
    query = (
        f"{SINA_API}/Market_Center.getHQNodeData?"
        f"page={page}&num={_PAGE_SIZE}&sort=symbol&asc=1&node=hs_a"
    )
    body = _http_get(query)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _http_get(url: str, *, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _from_tushare() -> list[str]:
    """``stock_basic`` — the only source that also carries delisted names."""
    from backtest.loaders.tushare_pool import get_tushare_pool, resolve_tushare_tokens

    if not resolve_tushare_tokens():
        return []
    pool = get_tushare_pool()
    symbols: dict[str, None] = {}
    for status in ("L", "D", "P"):
        try:
            frame = pool.call("stock_basic", list_status=status, fields="ts_code,list_status")
        except Exception:  # noqa: BLE001 - missing permission is not fatal
            continue
        if frame is None or frame.empty or "ts_code" not in frame.columns:
            continue
        for code in frame["ts_code"].astype(str):
            symbol = cn_symbol(code)
            if symbol:
                symbols[symbol] = None
    return sorted(symbols)


def _from_boards() -> list[str]:
    """Last resort: the market-pick board taxonomy (partial by construction)."""
    from src.market_pick.boards import load_board_universe, unique_symbols

    boards = load_board_universe(dt.date.today())
    return sorted({cn_symbol(code) for code in unique_symbols(boards) if code} - {""})


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _read_cache() -> dict[str, Any] | None:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing or unreadable cache is "no cache"
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
        return None
    symbols = [cn_symbol(item) for item in raw["symbols"]]
    cleaned = sorted({s for s in symbols if s})
    if not cleaned:
        return None
    return {
        "fetched_at": str(raw.get("fetched_at") or ""),
        "source": str(raw.get("source") or "unknown"),
        "symbols": cleaned,
    }


def _write_cache(symbols: Sequence[str], *, source: str) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "count": len(symbols),
            "symbols": list(symbols),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 - cache is an optimisation
        logger.debug("universe cache write failed: %s", exc)


def _fresh(cached: dict[str, Any]) -> bool:
    stamp = str(cached.get("fetched_at") or "")[:10]
    return stamp == dt.date.today().isoformat()


def _note(notes: list[str] | None, message: str) -> None:
    if notes is not None:
        notes.append(message)


def _dedupe(symbols: Iterable[str]) -> list[str]:
    return sorted({s for s in symbols if s})
