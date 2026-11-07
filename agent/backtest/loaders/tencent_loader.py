"""Tencent Finance loader: free, no-auth A-share data via HTTP API.

Uses Tencent's ``ifzq.gtimg.cn`` API, which is not blocked by eastmoney's CDN.
Covers: A-shares (SH/SZ).  No API token required.

API formats:
  daily   https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601595,day,2026-06-01,2026-06-13,500,qfq
  minute  https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh601595,m5,,320

The ``web.`` host resolves intermittently on some networks, so both hosts are
tried for every request.

Two fidelity caveats, both deliberate and both logged:

* **Minute bars are a sliding window.** ``mkline`` ignores any date range and
  returns the most recent ``limit`` bars (320 is the server-side maximum), so a
  historical intraday request yields only the tail.  ``clip_window`` reports the
  real coverage.
* **Minute bars are unadjusted** while the daily endpoint serves 前复权 prices.
  Tencent has no adjusted minute feed; mixing the two in one run would break
  price continuity across a dividend, so the interval is served as-is and the
  caller decides.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders._intervals import (
    clip_window,
    is_intraday,
    normalize_interval,
    resample_ohlcv,
)
from backtest.loaders._symbol_utils import _is_b_share, _is_cn_index
from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register
from backtest.loaders.source_health import is_cooling_down, record_failure, record_success

logger = logging.getLogger(__name__)

_BASE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_MKLINE_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn")

#: Tencent minute token per interval.  ``4H`` is absent: it is built from 60m.
_INTERVAL_MAP_MINUTE = {
    "1m": "m1",
    "5m": "m5",
    "15m": "m15",
    "30m": "m30",
    "1H": "m60",
}

#: Server-side maximum for ``mkline`` — the whole reason intraday coverage is
#: limited to a window rather than the requested range.
_MKLINE_MAX_BARS = 320
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://web.ifzq.gtimg.cn/",
}


def _get_json(url: str, timeout: int = 15) -> dict:
    """GET *url* and decode JSON, without inheriting a broken proxy env."""
    import urllib.request

    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_first_available(path: str, *, timeout: int = 15) -> dict | None:
    """Try each mkline host in turn; return the first JSON payload."""
    for host in _MKLINE_HOSTS:
        try:
            return _get_json(f"{host}{path}", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — DNS/proxy drift per host
            logger.debug("tencent host %s failed for %s: %s", host, path, exc)
    return None


def _is_a_share(code: str) -> bool:
    return code.upper().endswith((".SZ", ".SH"))


@register
class DataLoader:
    """Tencent Finance A-share OHLCV loader (free, HTTP, no auth)."""

    name = "tencent"
    markets = {"a_share"}
    requires_auth = False
    #: Intraday is a recent-window feed; ``4H`` is aggregated from 60m.
    SUPPORTED_INTERVALS = frozenset({"1D", "1m", "5m", "15m", "30m", "1H", "4H"})

    def is_available(self) -> bool:
        """Always available — uses plain HTTP."""
        return True

    def __init__(self) -> None:
        pass

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        del fields

        token = normalize_interval(interval)
        if token not in self.SUPPORTED_INTERVALS:
            logger.warning(
                "tencent supports %s; rejecting interval=%s",
                sorted(self.SUPPORTED_INTERVALS), interval,
            )
            return {}

        result: Dict[str, pd.DataFrame] = {}
        remaining = list(codes)
        for index, code in enumerate(remaining):
            if index > 0 and is_cooling_down(self.name):
                skipped = len(remaining) - index
                logger.warning(
                    "tencent circuit open; skipping %d remaining symbols", skipped
                )
                break
            if _is_b_share(code) or _is_cn_index(code):
                continue
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(code, start_date, end_date, token),
                )
                if df is not None and not df.empty:
                    result[code] = df
                    record_success(self.name)
            except Exception as exc:
                record_failure(self.name, exc)
                logger.warning("tencent failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str, interval: str = "1D",
    ) -> Optional[pd.DataFrame]:
        if not _is_a_share(code):
            return None

        parts = code.upper().split(".")
        symbol = parts[0]
        suffix = parts[1] if len(parts) > 1 else ""

        if suffix == "SH":
            tencent_code = f"sh{symbol}"
        elif suffix == "SZ":
            tencent_code = f"sz{symbol}"
        else:
            return None

        if is_intraday(interval):
            return self._fetch_minute(code, tencent_code, start_date, end_date, interval)
        return self._fetch_daily(tencent_code, start_date, end_date)

    def _fetch_daily(
        self, tencent_code: str, start_date: str, end_date: str,
    ) -> Optional[pd.DataFrame]:
        """Daily (前复权) bars."""
        path = (
            f"/appstock/app/fqkline/get?param={tencent_code},day,"
            f"{start_date},{end_date},500,qfq"
        )
        try:
            data = _get_json(f"https://web.ifzq.gtimg.cn{path}")
        except Exception as exc:  # noqa: BLE001 — try the alternate host
            logger.debug("tencent daily host failed (%s); retrying", exc)
            data = _get_first_available(path)
        if not data:
            return None

        # Response: {"code":0,"data":{"sh601595":{"day":[["2026-06-01","21.32",...], ...]}}}
        stock_data = data.get("data", {})
        if not stock_data:
            return None

        # Get the first (only) key
        stock_key = next(iter(stock_data), None)
        if not stock_key:
            return None

        # Try "day" first, then "qfqday" (forward-adjusted)
        klines = stock_data[stock_key].get("qfqday") or stock_data[stock_key].get("day")
        if not klines:
            return None

        # Each row: ["date", "open", "close", "high", "low", "volume"]
        rows = []
        for k in klines:
            if len(k) >= 6:
                rows.append({
                    "trade_date": k[0],
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                    "volume": float(k[5]),
                })

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        return df

    def _fetch_minute(
        self,
        code: str,
        tencent_code: str,
        start_date: str,
        end_date: str,
        interval: str,
    ) -> Optional[pd.DataFrame]:
        """Intraday bars from ``mkline`` — recent window, unadjusted."""
        target = "1H" if interval == "4H" else interval
        token = _INTERVAL_MAP_MINUTE.get(target)
        if token is None:
            logger.warning("tencent has no minute feed for %s", interval)
            return None

        path = f"/appstock/app/kline/mkline?param={tencent_code},{token},,{_MKLINE_MAX_BARS}"
        data = _get_first_available(path)
        if not data:
            return None

        series = data.get("data", {}).get(tencent_code, {}).get(token)
        if not series:
            return None

        # Each row: ["YYYYMMDDHHMM", open, close, high, low, volume, {}, amount]
        rows = []
        for k in series:
            if len(k) < 6:
                continue
            stamp = pd.to_datetime(str(k[0]), format="%Y%m%d%H%M", errors="coerce")
            if stamp is pd.NaT:
                continue
            rows.append({
                "trade_date": stamp,
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]),
            })
        if not rows:
            return None

        df = pd.DataFrame(rows).set_index("trade_date").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        if df.empty:
            return None

        df = clip_window(df, start_date, end_date, symbol=code, interval=interval)
        if df.empty:
            return None
        if interval == "4H":
            df = resample_ohlcv(df, "4H", symbol=code)
        return df
