"""Tushare loader for A-share daily and intraday bars plus optional fundamentals.

Supports ``interval``: 1D (default) / 1m / 5m / 15m / 30m / 1H.
Minute data uses ``pro.stk_mins()`` (Tushare points >= 2000).

All API traffic goes through :mod:`backtest.loaders.tushare_pool` so multiple
tokens can share load under the per-account RPM cap.
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders._symbol_utils import _is_etf_listed
from backtest.loaders.base import (
    cached_loader_fetch,
    loader_cache_get,
    loader_cache_put,
    validate_date_range,
)
from backtest.loaders.registry import register
from backtest.loaders.tushare_pool import (
    PoolApiProxy,
    get_tushare_pool,
    resolve_tushare_tokens,
)

logger = logging.getLogger(__name__)


def _is_index(code: str) -> bool:
    """Detect A-share index symbols (000xxx.SH, 000300.SH, 399xxx.SZ)."""
    upper = code.upper()
    if upper.endswith(".SH"):
        digits = upper.split(".")[0]
        return len(digits) == 6 and digits.isdigit() and digits.startswith("000")
    if upper.endswith(".SZ"):
        digits = upper.split(".")[0]
        return len(digits) == 6 and digits.isdigit() and digits.startswith("399")
    return False


def _is_hk_equity(code: str) -> bool:
    """Detect Hong Kong equity symbols (e.g. 00700.HK)."""
    return code.upper().endswith(".HK")


def _is_us_equity(code: str) -> bool:
    """Detect US equity symbols (e.g. AAPL.US)."""
    return code.upper().endswith(".US")


def _is_crypto(code: str) -> bool:
    """Detect crypto symbols (e.g. BTC-USDT, ETH/USDT)."""
    upper = code.upper()
    return upper.endswith("-USDT") or upper.endswith("/USDT")


def _is_batchable_stock(code: str) -> bool:
    """True for plain A-share stocks that share the ``daily`` endpoint."""
    if (
        _is_us_equity(code)
        or _is_crypto(code)
        or _is_etf_listed(code)
        or _is_index(code)
        or _is_hk_equity(code)
    ):
        return False
    return True


def _daily_batch_size() -> int:
    from src.config.accessor import get_env_config

    size = int(get_env_config().data.tushare_daily_batch_size or 20)
    return max(size, 1)


def _normalize_daily_ohlcv(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Convert a raw Tushare daily frame into the loader's OHLCV layout.

    Keeps ``amount`` when the endpoint provides it — factor-bench CSI300 needs
    it for CN VWAP, and leaving it in the frame lets the shared per-symbol
    cache serve both backtests and alpha benches.
    """
    if df is None or df.empty:
        return None
    frame = df.sort_values("trade_date")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date")
    frame = frame.rename(columns={"vol": "volume"})
    keep = ["open", "high", "low", "close", "volume"]
    if "amount" in frame.columns:
        keep.append("amount")
    for col in keep:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    ohlcv = frame[keep].dropna(subset=["open", "high", "low", "close"])
    return ohlcv if not ohlcv.empty else None


@register
class DataLoader:
    """Tushare-backed OHLCV loader."""

    name = "tushare"
    markets = {"a_share", "futures", "fund"}
    requires_auth = True

    def is_available(self) -> bool:
        """Available when at least one non-placeholder Tushare token is set."""
        return bool(resolve_tushare_tokens())

    def __init__(self) -> None:
        """Bind to the shared multi-token pool (tests may overwrite ``self.api``)."""
        self._pool = get_tushare_pool()
        self.api = PoolApiProxy(self._pool)

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch A-share bars via Tushare API.

        Args:
            codes: Stock codes (e.g. ``000001.SZ``).
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            fields: Extra fundamental columns (daily only).
            interval: Bar size (1D/1m/5m/15m/30m/1H), default ``1D``.

        Returns:
            Mapping code -> OHLCV DataFrame.
        """
        validate_date_range(start_date, end_date)

        if interval != "1D":
            return self._fetch_minutes(codes, start_date, end_date, interval)

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        cache_fields = list(fields or [])
        result: Dict[str, pd.DataFrame] = {}

        stock_codes = [code for code in codes if _is_batchable_stock(code)]
        special_codes = [code for code in codes if not _is_batchable_stock(code)]

        # ETF / index / HK keep the per-symbol path (different endpoints).
        for code in special_codes:
            df = self._fetch_one_cached(
                code, start_date, end_date, sd, ed, cache_fields
            )
            if df is not None and not df.empty:
                result[code] = df

        # Plain A-share stocks: serve cache hits, then batch-fetch the rest so
        # N symbols become ceil(N / batch_size) daily() calls instead of N.
        pending: List[str] = []
        for code in stock_codes:
            cached = loader_cache_get(
                source=self.name,
                symbol=code,
                timeframe="1D",
                start_date=start_date,
                end_date=end_date,
                fields=cache_fields,
            )
            if cached is not None and not cached.empty:
                result[code] = cached
            else:
                pending.append(code)

        batch_size = _daily_batch_size()
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            fetched = self._fetch_stock_daily_batch(batch, sd, ed)
            if cache_fields:
                fetched = self._merge_basic_fields(
                    fetched, batch, start_date, end_date, cache_fields
                )
            for code in batch:
                frame = fetched.get(code)
                if frame is None or frame.empty:
                    continue
                loader_cache_put(
                    source=self.name,
                    symbol=code,
                    timeframe="1D",
                    start_date=start_date,
                    end_date=end_date,
                    fields=cache_fields,
                    frame=frame,
                )
                result[code] = frame

        return result

    def _fetch_one_cached(
        self,
        code: str,
        start_date: str,
        end_date: str,
        sd: str,
        ed: str,
        cache_fields: List[str],
    ) -> Optional[pd.DataFrame]:
        """Per-symbol fetch used for non-batchable instruments."""

        def _fetch_one() -> Optional[pd.DataFrame]:
            try:
                df = self._fetch_daily_frame(code, sd, ed)
                if df is None:
                    return None
                merged = self._merge_basic_fields(
                    {code: df}, [code], start_date, end_date, cache_fields
                )
                return merged.get(code)
            except Exception as exc:
                logger.warning("failed to fetch %s: %s", code, exc)
                return None

        return cached_loader_fetch(
            source=self.name,
            symbol=code,
            timeframe="1D",
            start_date=start_date,
            end_date=end_date,
            fields=cache_fields,
            fetch=_fetch_one,
        )

    def _fetch_stock_daily_batch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch several A-share stocks in one ``daily`` call when possible."""
        if not codes:
            return {}
        if len(codes) == 1:
            try:
                frame = self._fetch_daily_frame(codes[0], start_date, end_date)
            except Exception as exc:
                logger.warning("failed to fetch %s: %s", codes[0], exc)
                return {}
            return {codes[0]: frame} if frame is not None else {}

        joined = ",".join(codes)
        try:
            raw = self.api.daily(ts_code=joined, start_date=start_date, end_date=end_date)
        except Exception as exc:
            # Batch reject (some tiers disallow multi-code): fall back per symbol.
            logger.warning(
                "batched daily fetch failed (%s); falling back to per-symbol: %s",
                joined,
                exc,
            )
            return self._fetch_stock_daily_each(codes, start_date, end_date)

        if raw is None or raw.empty:
            # Some accounts accept the multi-code POST but return an empty
            # frame instead of an error. Retry one-by-one before giving up.
            logger.warning(
                "batched daily fetch returned empty for %d symbols; falling back to per-symbol",
                len(codes),
            )
            return self._fetch_stock_daily_each(codes, start_date, end_date)

        result: Dict[str, pd.DataFrame] = {}
        if "ts_code" not in raw.columns:
            # Single-code response shape without ts_code — only valid for one symbol.
            if len(codes) == 1:
                normalized = _normalize_daily_ohlcv(raw)
                if normalized is not None:
                    result[codes[0]] = normalized
            return result

        for code, group in raw.groupby("ts_code"):
            normalized = _normalize_daily_ohlcv(group)
            if normalized is None:
                logger.warning("tushare returned empty for %s via daily", code)
                continue
            result[str(code)] = normalized

        for code in codes:
            if code not in result:
                logger.warning("tushare returned empty for %s via daily", code)
        return result

    def _fetch_stock_daily_each(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch A-share stocks one ``daily`` call at a time."""
        out: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                frame = self._fetch_daily_frame(code, start_date, end_date)
            except Exception as exc:
                logger.warning("failed to fetch %s: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                out[code] = frame
        return out

    def _fetch_daily_frame(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch and normalize one daily OHLCV frame, routing by symbol type."""
        if _is_us_equity(code) or _is_crypto(code):
            logger.warning("tushare does not support %s (US/crypto); skipping", code)
            return None

        if _is_etf_listed(code):
            endpoint_name = "fund_daily"
            df = self.api.fund_daily(ts_code=code, start_date=start_date, end_date=end_date)
        elif _is_index(code):
            endpoint_name = "index_daily"
            df = self.api.index_daily(ts_code=code, start_date=start_date, end_date=end_date)
        elif _is_hk_equity(code):
            endpoint_name = "hk_daily"
            df = self.api.hk_daily(ts_code=code, start_date=start_date, end_date=end_date)
        else:
            endpoint_name = "daily"
            df = self.api.daily(ts_code=code, start_date=start_date, end_date=end_date)

        if df is None or df.empty:
            logger.warning("tushare returned empty for %s via %s", code, endpoint_name)
            return None
        return _normalize_daily_ohlcv(df)

    def _merge_basic_fields(
        self,
        result: Dict[str, pd.DataFrame],
        codes: List[str],
        start_date: str,
        end_date: str,
        fields: Optional[List[str]],
    ) -> Dict[str, pd.DataFrame]:
        """Merge fundamental columns from daily_basic API.

        Args:
            result: Existing OHLCV frames.
            codes: All requested codes.
            start_date: Start date.
            end_date: End date.
            fields: Extra column names from daily_basic.

        Returns:
            Updated result map.
        """
        if not fields:
            return result

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        active_codes = [c for c in codes if c in result]

        for code in active_codes:
            if (
                _is_etf_listed(code)
                or _is_index(code)
                or _is_hk_equity(code)
                or _is_us_equity(code)
                or _is_crypto(code)
            ):
                # daily_basic is stock-only; skip fundamental enrichment for non-stock symbols
                continue
            try:
                basic = self.api.daily_basic(
                    ts_code=code,
                    start_date=sd,
                    end_date=ed,
                    fields="ts_code,trade_date," + ",".join(fields),
                )
                if basic is not None and not basic.empty:
                    basic["trade_date"] = pd.to_datetime(basic["trade_date"])
                    basic = basic.set_index("trade_date").sort_index()
                    for f in fields:
                        if f in basic.columns:
                            result[code][f] = basic[f]
            except Exception as exc:
                logger.warning("daily_basic for %s failed: %s", code, exc)

        return result

    def _fetch_minutes(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        interval: str,
    ) -> Dict[str, pd.DataFrame]:
        """Intraday bars via stk_mins.

        Args:
            codes: Stock codes.
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            interval: Minute bar (1m/5m/15m/30m/1H).

        Returns:
            Mapping code -> DataFrame.
        """
        freq_map = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1H": "60min"}
        freq = freq_map.get(interval)
        if not freq:
            logger.error("unsupported Tushare interval: %s", interval)
            return {}

        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        result: Dict[str, pd.DataFrame] = {}

        for code in codes:
            if _is_etf_listed(code):
                # tushare has no fund_mins endpoint; ETF intraday is unavailable
                logger.warning("tushare does not support intraday data for %s (ETF); skipping", code)
                continue
            if _is_index(code) or _is_hk_equity(code) or _is_us_equity(code) or _is_crypto(code):
                sym_type = (
                    "index" if _is_index(code)
                    else "HK" if _is_hk_equity(code)
                    else "US" if _is_us_equity(code)
                    else "crypto"
                )
                logger.warning("tushare does not support intraday data for %s (%s); skipping", code, sym_type)
                continue
            try:
                df = self.api.stk_mins(ts_code=code, freq=freq, start_date=sd, end_date=ed)
                if df is None or df.empty:
                    logger.warning("empty Tushare minute data: %s (points >= 2000 required)", code)
                    continue
                df = df.sort_values("trade_time")
                df["trade_date"] = pd.to_datetime(df["trade_time"])
                df = df.set_index("trade_date")
                df = df.rename(columns={"vol": "volume"})
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                ohlcv = df[["open", "high", "low", "close", "volume"]].dropna(
                    subset=["open", "high", "low", "close"]
                )
                result[code] = ohlcv
            except Exception as exc:
                logger.warning("failed to fetch minute data %s: %s", code, exc)
        return result
