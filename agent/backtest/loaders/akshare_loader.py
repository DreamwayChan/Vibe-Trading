"""AKShare loader: free, no-auth data for A-shares, US, HK, futures, forex, macro.

AKShare (https://github.com/akfamily/akshare) is a completely free financial
data aggregator covering Chinese and global markets.  No API token required.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders._intervals import clip_window, is_intraday, resample_ohlcv
from backtest.loaders._symbol_utils import _is_etf_listed
from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register
from backtest.loaders.source_health import record_failure, record_success

logger = logging.getLogger(__name__)

_INTERVAL_MAP_DAILY = {
    "1D": "daily",
    "1W": "weekly",
    "1M": "monthly",
}

# Eastmoney minute endpoint takes the period as a bare minute count.  ``4H`` is
# absent on purpose: no free endpoint serves it, so it is built by aggregating
# 60-minute bars.
_INTERVAL_MAP_MINUTE = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1H": "60",
}


def _is_a_share(code: str) -> bool:
    return code.upper().endswith((".SZ", ".SH", ".BJ"))


def _is_hk(code: str) -> bool:
    return code.upper().endswith(".HK")


def _is_us(code: str) -> bool:
    return code.upper().endswith(".US")


def _is_crypto(code: str) -> bool:
    return "-USDT" in code.upper() or "/USDT" in code.upper()





def _is_forex(code: str) -> bool:
    """Detect forex pairs by matching against AKShare's symbol_market_map.

    Issue #54 — forex symbols (EURUSD, GBPUSD, etc.) have no exchange suffix
    and previously fell through to the A-share endpoint.
    """
    # Accept the canonical slash form (EUR/USD) too, so the forex fallback
    # chain (mt5 → akshare) actually engages for project-style codes.
    upper = code.upper().removesuffix(".FX").replace("/", "")
    try:
        from akshare.forex.cons import symbol_market_map
    except Exception:
        return False
    return upper in symbol_market_map


@register
class DataLoader:
    """AKShare universal OHLCV loader (free, no auth)."""

    name = "akshare"
    markets = {"a_share", "us_equity", "hk_equity", "futures", "fund", "macro", "forex"}
    requires_auth = False
    # Intraday is A-share only (Eastmoney/Sina): US and HK stay on daily bars.
    SUPPORTED_INTERVALS = frozenset({"1D", "1W", "1M", "1m", "5m", "15m", "30m", "1H", "4H"})

    def is_available(self) -> bool:
        """Available if akshare is installed."""
        try:
            import akshare  # noqa: F401
            return True
        except ImportError:
            return False

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
        """Fetch OHLCV data via AKShare.

        Args:
            codes: Symbol list.
            start_date: YYYY-MM-DD.
            end_date: YYYY-MM-DD.
            interval: Bar size. ``1D``/``1W``/``1M`` for every market;
                ``1m``/``5m``/``15m``/``30m``/``1H``/``4H`` for A-shares only.
            fields: Ignored.

        Returns:
            Mapping symbol -> OHLCV DataFrame.
        """
        validate_date_range(start_date, end_date)

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(code, start_date, end_date, interval),
                )
                if df is not None and not df.empty:
                    result[code] = df
                    record_success(self.name)
            except Exception as exc:
                record_failure(self.name, exc)
                logger.warning("akshare failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str, interval: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch a single symbol."""
        import akshare as ak

        # ETF check must precede A-share — 518880.SH ends with .SH but is an ETF.
        if _is_etf_listed(code):
            if is_intraday(interval):
                # fund_etf_hist_sina is daily-only; Eastmoney's minute endpoint
                # serves ETFs through the same kline API as stocks.
                return self._fetch_a_share_minute(
                    ak, code, start_date, end_date, interval, allow_sina=False
                )
            return self._fetch_etf(ak, code, start_date, end_date)
        if _is_a_share(code):
            return self._fetch_a_share(ak, code, start_date, end_date, interval)
        if _is_us(code):
            return self._fetch_us(ak, code, start_date, end_date)
        if _is_hk(code):
            return self._fetch_hk(ak, code, start_date, end_date)
        if _is_forex(code):
            return self._fetch_forex(ak, code, start_date, end_date)
        # Default: try A-share
        return self._fetch_a_share(ak, code, start_date, end_date, interval)

    def _fetch_a_share(
        self, ak, code: str, start_date: str, end_date: str, interval: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch A-share daily bars, or delegate to the minute path."""
        if is_intraday(interval):
            return self._fetch_a_share_minute(ak, code, start_date, end_date, interval)
        symbol = code.split(".")[0]
        period = _INTERVAL_MAP_DAILY.get(interval)
        if period is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; akshare a-share supports "
                f"{sorted(_INTERVAL_MAP_DAILY)}"
            )
        sd = start_date.replace("-", "")
        ed = end_date.replace("-", "")
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period=period,
            start_date=sd,
            end_date=ed,
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        return self._normalize(df, date_col="日期")

    def _fetch_a_share_minute(
        self,
        ak,
        code: str,
        start_date: str,
        end_date: str,
        interval: str,
        *,
        allow_sina: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Intraday A-share bars: Eastmoney first, Sina as the fallback.

        Neither free endpoint serves arbitrary history: Eastmoney caps 1-minute
        data at a few recent sessions, and Sina returns a fixed window of the
        most recent bars with no date parameters at all.  Both therefore hand
        back whatever they have and :func:`clip_window` reports the real
        coverage, so a short frame never reads as "no trades".
        """
        target = "1H" if interval == "4H" else interval
        period = _INTERVAL_MAP_MINUTE.get(target)
        if period is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; akshare a-share intraday "
                f"supports {sorted(_INTERVAL_MAP_MINUTE)} (4H is built from 1H)"
            )

        symbol = code.split(".")[0]
        frame = self._minute_eastmoney(ak, symbol, start_date, end_date, period, code)
        if frame is None and allow_sina:
            frame = self._minute_sina(ak, code, period)
        if frame is None or frame.empty:
            return None

        frame = clip_window(
            frame, start_date, end_date, symbol=code, interval=interval
        )
        if frame.empty:
            return None
        if interval == "4H":
            frame = resample_ohlcv(frame, "4H", symbol=code)
        return frame

    def _minute_eastmoney(
        self, ak, symbol: str, start_date: str, end_date: str, period: str,
        code: str,
    ) -> Optional[pd.DataFrame]:
        """Minute bars from Eastmoney (``stock_zh_a_hist_min_em``)."""
        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date=f"{start_date} 09:30:00",
                end_date=f"{end_date} 15:00:00",
                period=period,
                adjust="qfq",
            )
        except Exception as exc:  # noqa: BLE001 — network/proxy drift, try Sina
            logger.info("eastmoney minute endpoint unavailable for %s: %s", code, exc)
            return None
        if df is None or df.empty:
            return None
        date_col = "时间" if "时间" in df.columns else "日期"
        return self._normalize(df, date_col=date_col)

    def _minute_sina(self, ak, code: str, period: str) -> Optional[pd.DataFrame]:
        """Minute bars from Sina (``stock_zh_a_minute``), a sliding window."""
        digits, _, suffix = code.upper().partition(".")
        if suffix not in {"SH", "SZ"}:
            return None
        try:
            df = ak.stock_zh_a_minute(
                symbol=f"{suffix.lower()}{digits}", period=period, adjust="qfq"
            )
        except Exception as exc:  # noqa: BLE001 — endpoint drift is not fatal
            logger.warning("sina minute endpoint failed for %s: %s", code, exc)
            return None
        if df is None or df.empty:
            return None
        date_col = "day" if "day" in df.columns else "日期"
        return self._normalize(df, date_col=date_col)

    def _fetch_us(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch US stock via stock_us_hist."""
        symbol = code.replace(".US", "")
        # akshare uses the format like "105.AAPL" for NASDAQ
        # Try common prefixes
        for prefix in ["105.", "106.", ""]:
            try:
                df = ak.stock_us_hist(
                    symbol=f"{prefix}{symbol}",
                    period="daily",
                    start_date=start_date.replace("-", ""),
                    end_date=end_date.replace("-", ""),
                    adjust="qfq",
                )
                if df is not None and not df.empty:
                    return self._normalize(df, date_col="日期")
            except Exception:
                continue
        return None

    def _fetch_etf(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch exchange-listed ETF / LOF via fund_etf_hist_sina.

        Sina symbol format is ``sh518880`` / ``sz159915``. The endpoint returns
        the full history; we filter to the requested window after fetching.
        """
        digits, _, suffix = code.upper().partition(".")
        symbol = f"{suffix.lower()}{digits}"
        df = ak.fund_etf_hist_sina(symbol=symbol)
        if df is None or df.empty:
            return None
        df = self._normalize(df, date_col="date")
        # fund_etf_hist_sina returns full history — clip to window.
        return df.loc[start_date:end_date]

    def _fetch_forex(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch forex pair via forex_hist_em.

        Columns returned are 日期 / 代码 / 名称 / 今开 / 最新价 / 最高 / 最低 / 振幅
        — note ``最新价`` (latest) plays the role of close. Volume isn't reported,
        so we synthesize a zero column to satisfy the OHLCV contract.
        """
        symbol = code.upper().removesuffix(".FX").replace("/", "")
        df = ak.forex_hist_em(symbol=symbol)
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "日期": "trade_date",
            "今开": "open",
            "最新价": "close",
            "最高": "high",
            "最低": "low",
        })
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        df["volume"] = 0.0
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        return df.loc[start_date:end_date]

    def _fetch_hk(self, ak, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Fetch HK stock via stock_hk_hist."""
        symbol = code.replace(".HK", "").zfill(5)
        df = ak.stock_hk_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        return self._normalize(df, date_col="日期")

    @staticmethod
    def _normalize(df: pd.DataFrame, date_col: str = "日期") -> pd.DataFrame:
        """Normalize AKShare DataFrame to standard OHLCV schema.

        AKShare Chinese column names: 日期, 开盘, 最高, 最低, 收盘, 成交量
        AKShare English column names: date, open, high, low, close, volume
        """
        col_map_cn = {"开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume"}
        col_map_en = {"date": "trade_date", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}

        if date_col in df.columns:
            df = df.rename(columns={date_col: "trade_date"})
        elif "date" in df.columns:
            df = df.rename(columns={"date": "trade_date"})

        # Try Chinese column names first, then English
        if "开盘" in df.columns:
            df = df.rename(columns=col_map_cn)
        else:
            df = df.rename(columns=col_map_en)

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        ohlcv_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[ohlcv_cols].dropna(subset=["open", "high", "low", "close"])
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return df
