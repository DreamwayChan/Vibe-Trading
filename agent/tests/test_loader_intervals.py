"""Tests for the bar-interval contract and the intraday loader paths.

Three behaviours are pinned here:

1. Interval spelling never silently changes meaning (``1M`` month vs ``1m``
   minute), and a loader's declared granularity is authoritative.
2. Resampling is anchored to the session, so the A-share lunch break cannot
   split a bucket.
3. The A-share loaders that used to answer an intraday request with daily bars
   now either serve real minute bars or refuse.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.loaders._intervals import (
    clip_window,
    interval_minutes,
    is_intraday,
    native_interval,
    normalize_interval,
    resample_ohlcv,
    supports_interval,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _session_index(days: tuple[str, ...] = ("2024-01-02",), *, step: str = "5min"):
    """A-share session stamps: 09:35-11:30 and 13:05-15:00, bar-end labelled."""
    stamps = []
    for day in days:
        morning = pd.date_range(f"{day} 09:35", f"{day} 11:30", freq=step)
        afternoon = pd.date_range(f"{day} 13:05", f"{day} 15:00", freq=step)
        stamps.extend(list(morning) + list(afternoon))
    return pd.DatetimeIndex(stamps)


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    count = len(index)
    return pd.DataFrame(
        {
            "open": range(count),
            "high": [value + 1 for value in range(count)],
            "low": [value - 1 for value in range(count)],
            "close": [value + 0.5 for value in range(count)],
            "volume": [1] * count,
        },
        index=index,
    ).astype(float)


# ---------------------------------------------------------------------------
# Interval vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1D", "1D"),
        ("1d", "1D"),
        ("day", "1D"),
        ("daily", "1D"),
        ("5m", "5m"),
        ("5min", "5m"),
        ("60m", "1H"),
        ("1h", "1H"),
        ("4h", "4H"),
        # Case matters for exactly one pair: "1M" is monthly, "1m" is minute.
        ("1M", "1M"),
        ("1m", "1m"),
    ],
)
def test_normalize_interval(raw, expected):
    assert normalize_interval(raw) == expected


def test_is_intraday():
    assert is_intraday("5m") is True
    assert is_intraday("4H") is True
    assert is_intraday("1D") is False


def test_interval_minutes():
    assert interval_minutes("1m") == 1
    assert interval_minutes("1H") == 60
    assert interval_minutes("4H") == 240
    assert interval_minutes("1W") is None


def test_supports_interval_undeclared_loader_is_permissive():
    """Loaders without a declaration keep their own interval handling."""

    class _Legacy:
        pass

    assert supports_interval(_Legacy, "5m") is True


def test_supports_interval_declaration_is_authoritative():
    class _DailyOnly:
        SUPPORTED_INTERVALS = frozenset({"1D"})

    assert supports_interval(_DailyOnly, "1D") is True
    assert supports_interval(_DailyOnly, "5m") is False


def test_supports_interval_accepts_spellings():
    class _Minute:
        SUPPORTED_INTERVALS = frozenset({"1D", "1H"})

    assert supports_interval(_Minute, "60m") is True


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def test_native_interval_detection():
    assert native_interval(_frame(_session_index(step="5min"))) == "5m"
    assert native_interval(_frame(_session_index(step="15min"))) == "15m"


def test_resample_one_hour_follows_session_boundaries():
    frame = _frame(_session_index())
    out = resample_ohlcv(frame, "1H", symbol="TEST")

    assert len(out) == 4  # 09:30-10:30, 10:30-11:30, 13:00-14:00, 14:00-15:00
    stamps = [stamp.strftime("%H:%M") for stamp in out.index]
    assert stamps == ["10:30", "11:30", "14:00", "15:00"]
    # A 5-minute source has 12 bars per hour bucket.
    assert list(out["volume"]) == [12.0, 12.0, 12.0, 12.0]
    assert out["open"].iloc[0] == frame["open"].iloc[0]
    assert out["close"].iloc[0] == frame["close"].iloc[11]


def test_resample_four_hour_is_one_bar_per_session():
    """A 4H bar must span the whole session, not stop at the lunch break."""
    frame = _frame(_session_index(days=("2024-01-02", "2024-01-03")))
    out = resample_ohlcv(frame, "4H", symbol="TEST")

    assert len(out) == 2
    assert [stamp.strftime("%H:%M") for stamp in out.index] == ["15:00", "15:00"]
    assert list(out["volume"]) == [48.0, 48.0]


def test_resample_daily_from_minutes():
    frame = _frame(_session_index(days=("2024-01-02", "2024-01-03")))
    out = resample_ohlcv(frame, "1D", symbol="TEST")

    assert len(out) == 2
    assert out["high"].iloc[0] == frame["high"].iloc[:48].max()
    assert out["low"].iloc[1] == frame["low"].iloc[48:].min()


def test_resample_refuses_to_upsample(caplog):
    frame = _frame(_session_index(step="15min"))
    out = resample_ohlcv(frame, "5m", symbol="TEST")

    assert len(out) == len(frame)
    assert "cannot upsample" in caplog.text


def test_resample_same_interval_is_identity():
    frame = _frame(_session_index())
    assert resample_ohlcv(frame, "5m", symbol="TEST") is frame


def test_resample_surplus_auction_bar_stays_in_four_hours(caplog):
    """Tushare emits 49 bars a session (09:30 auction + 48); 1H must stay at 4.

    Unclamped, twelve-bars-per-bucket turns the extra bar into a fifth hourly
    bucket holding a single bar, which inflates every count that follows.
    """
    stamps = []
    for day in ("2024-01-02", "2024-01-03"):
        stamps.append(pd.Timestamp(f"{day} 09:30"))
        stamps += list(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min"))
        stamps += list(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min"))
    index = pd.DatetimeIndex(stamps)
    frame = _frame(index)

    out = resample_ohlcv(frame, "1H", symbol="TEST")

    assert len(out) == 8  # 4 per session, not 5
    assert out["volume"].sum() == pytest.approx(frame["volume"].sum())
    daily = resample_ohlcv(frame, "4H", symbol="TEST")
    assert len(daily) == 2


def test_resample_empty_is_safe():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert resample_ohlcv(empty, "1H").empty


# ---------------------------------------------------------------------------
# Coverage reporting
# ---------------------------------------------------------------------------


def test_clip_window_trims_and_warns_on_short_coverage(caplog):
    frame = _frame(_session_index(days=("2024-01-02", "2024-01-03")))
    out = clip_window(frame, "2023-11-01", "2024-01-03", symbol="TEST", interval="5m")

    assert len(out) == len(frame)
    # Asked for two months, the feed starts in January: that must be said.
    assert "only cover" in caplog.text


def test_clip_window_quiet_when_window_is_covered(caplog):
    caplog.clear()
    frame = _frame(_session_index(days=("2024-01-02",)))
    out = clip_window(frame, "2024-01-02", "2024-01-02", symbol="TEST", interval="5m")

    assert len(out) == len(frame)
    assert "only cover" not in caplog.text


def test_clip_window_returns_empty_outside_window():
    frame = _frame(_session_index(days=("2024-01-02",)))
    out = clip_window(frame, "2024-03-01", "2024-03-02", symbol="TEST", interval="5m")
    assert out.empty


# ---------------------------------------------------------------------------
# Loader declarations
# ---------------------------------------------------------------------------


def test_akshare_declares_intraday():
    from backtest.loaders.akshare_loader import DataLoader as AkshareLoader

    for interval in ("1m", "5m", "15m", "30m", "1H", "4H", "1D"):
        assert supports_interval(AkshareLoader, interval), interval


def test_baostock_refuses_one_minute():
    from backtest.loaders.baostock_loader import DataLoader as BaostockLoader

    assert supports_interval(BaostockLoader, "5m") is True
    assert supports_interval(BaostockLoader, "1H") is True
    assert supports_interval(BaostockLoader, "1m") is False
    assert supports_interval(BaostockLoader, "1D") is True


def test_tencent_declares_intraday():
    from backtest.loaders.tencent_loader import DataLoader as TencentLoader

    for interval in ("1m", "5m", "1H", "4H"):
        assert supports_interval(TencentLoader, interval), interval


def test_sina_is_daily_only():
    from backtest.loaders.sina_loader import DataLoader as SinaLoader

    assert supports_interval(SinaLoader, "1D") is True
    assert supports_interval(SinaLoader, "5m") is False


# ---------------------------------------------------------------------------
# Source-specific parsing (no network)
# ---------------------------------------------------------------------------


def test_baostock_minute_rows_build_timestamps():
    from backtest.loaders.baostock_loader import _normalize_minute_rows

    rows = [
        ["2024-01-02", "20240102093500000", "1.0", "1.1", "0.9", "1.05", "100", "1050"],
        ["2024-01-02", "20240102094000000", "1.05", "1.2", "1.0", "1.15", "120", "1260"],
    ]
    frame = _normalize_minute_rows(rows)

    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp("2024-01-02 09:35")
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["volume"].iloc[1] == 120.0


def test_tencent_minute_parsing(monkeypatch):
    from backtest.loaders import tencent_loader

    payload = {
        "data": {
            "sh600519": {
                "m5": [
                    ["202401020935", "1.0", "1.05", "1.10", "0.95", "100", {}, "105"],
                    ["202401020940", "1.05", "1.15", "1.20", "1.00", "120", {}, "126"],
                ]
            }
        }
    }
    monkeypatch.setattr(tencent_loader, "_get_first_available", lambda path, **_: payload)

    loader = tencent_loader.DataLoader()
    frame = loader._fetch_minute("600519.SH", "sh600519", "2024-01-02", "2024-01-02", "5m")

    assert frame is not None
    assert len(frame) == 2
    assert frame.index[0] == pd.Timestamp("2024-01-02 09:35")
    assert frame["close"].iloc[1] == 1.15


def test_tencent_rejects_unknown_interval():
    from backtest.loaders.tencent_loader import DataLoader as TencentLoader

    assert TencentLoader().fetch(["600519.SH"], "2024-01-01", "2024-01-10", interval="1W") == {}
