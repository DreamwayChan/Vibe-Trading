"""Tests for tick-level trade direction (parsing, aggregation, storage).

The feed is the only free source of 主动买/主动卖, so the two things that must
not silently break are the side classification and the session boundary: the
closing call auction prints as one batch stamped 15:00:01 and *is* part of the
day's volume, while 15:01+ prints are a separate market segment.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from src.data import ticks as tk


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_TRADE_DATE = "2026-10-30"


def _payload(*records: str) -> str:
    return 'v_detail_data_sh600519=[0,"' + "|".join(records) + '"];'


def test_parse_page_extracts_records():
    body = _payload(
        "0/09:25:01/1363.00/-1.88/1534/209084200/S",
        "1/09:30:02/1357.01/-5.99/87/11806953/S",
    )
    rows = tk._parse_page(body)
    assert len(rows) == 2
    assert rows[0][1] == "09:25:01"
    assert rows[1][6] == "S"


def test_parse_page_empty_payload():
    assert tk._parse_page('v_detail_data_sh600519=[0,""];') == []


def test_tencent_code_mapping():
    assert tk._tencent_code("600519.SH") == "sh600519"
    assert tk._tencent_code("000001.SZ") == "sz000001"
    assert tk._tencent_code("830799.BJ") == "bj830799"
    assert tk._tencent_code("BTC-USDT") is None


def _ticks(rows: list[tuple[str, float, int, str]]) -> pd.DataFrame:
    """Build a frame shaped like :func:`fetch_ticks` output."""
    frame = pd.DataFrame(
        [
            {
                "seq": str(i),
                "time": pd.Timestamp(f"{_TRADE_DATE} {clock}"),
                "price": price,
                "change": 0.0,
                "lots": lots,
                "amount": lots * 100.0 * price,
                "side": side,
            }
            for i, (clock, price, lots, side) in enumerate(rows)
        ]
    )
    frame["volume"] = frame["lots"] * 100.0
    frame["is_auction"] = frame["time"].dt.strftime("%H:%M:%S") < "09:30:00"
    frame.attrs["complete"] = True
    return frame


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_active_buy_ratio_uses_directional_amount_only():
    frame = _ticks(
        [
            ("09:30:01", 10.0, 100, "B"),
            ("09:30:02", 10.0, 300, "S"),
            ("09:30:03", 10.0, 600, "M"),
        ]
    )
    row = tk.aggregate(frame, "600519.SH")
    # buy 100k vs sell 300k → 0.25, unaffected by the 600k neutral print.
    assert row["buy_amount"] == pytest.approx(100_000.0)
    assert row["sell_amount"] == pytest.approx(300_000.0)
    assert row["active_buy_ratio"] == pytest.approx(0.25)
    assert row["net_active_amount"] == pytest.approx(-200_000.0)
    assert row["neutral_volume"] == pytest.approx(60_000.0)


def test_closing_auction_counts_as_session_volume():
    """The 15:00:01 batch is the closing call auction, not an after-hours trade."""
    frame = _ticks(
        [
            ("14:59:59", 10.0, 100, "B"),
            ("15:00:01", 10.0, 2546, "M"),  # closing auction batch
            ("15:05:01", 10.0, 10, "M"),    # 盘后固定价格交易
        ]
    )
    row = tk.aggregate(frame, "600519.SH")
    assert row["volume"] == pytest.approx((100 + 2546) * 100.0)
    assert row["close_auction_volume"] == pytest.approx(2546 * 100.0)
    assert row["post_close_volume"] == pytest.approx(10 * 100.0)
    assert row["last_time"] == "15:00:01"


def test_open_auction_is_separated():
    frame = _ticks([("09:25:01", 10.0, 1000, "S"), ("09:30:01", 10.0, 50, "B")])
    row = tk.aggregate(frame, "600519.SH")
    assert row["open_auction_volume"] == pytest.approx(100_000.0)
    assert row["trades"] == 2


def test_big_trade_columns_use_notional_threshold():
    frame = _ticks(
        [
            ("09:30:01", 100.0, 50, "B"),     # 500k — below the cut-off
            ("09:30:02", 100.0, 500, "B"),    # 5m — big buy
            ("09:30:03", 100.0, 300, "S"),    # 3m — big sell
        ]
    )
    row = tk.aggregate(frame, "600519.SH")
    assert row["big_trade_count"] == 2
    assert row["big_net_amount"] == pytest.approx(2_000_000.0)
    assert row["big_amount_ratio"] == pytest.approx(8_000_000.0 / 8_500_000.0)


def test_aggregate_empty_returns_empty_mapping():
    assert tk.aggregate(pd.DataFrame(), "600519.SH") == {}


def test_aggregate_only_post_close_returns_empty():
    frame = _ticks([("15:05:01", 10.0, 10, "M")])
    assert tk.aggregate(frame, "600519.SH") == {}


def test_incomplete_pagination_is_flagged():
    frame = _ticks([("09:30:01", 10.0, 100, "B")])
    frame.attrs["complete"] = False
    row = tk.aggregate(frame, "600519.SH")
    assert row["prints_complete"] is False


# ---------------------------------------------------------------------------
# Volume reconciliation
# ---------------------------------------------------------------------------


def test_volume_deviation_matches_pct():
    # 59,931手 of prints against a 59,927手 bar is a 0.007% gap.
    assert tk.volume_deviation(5_993_100.0, 59_927.0) == pytest.approx(0.0067, abs=1e-3)


def test_volume_deviation_without_reference():
    assert tk.volume_deviation(1_000.0, None) is None
    assert tk.volume_deviation(1_000.0, 0) is None


# ---------------------------------------------------------------------------
# Session guards
# ---------------------------------------------------------------------------


def test_session_is_complete_after_close_on_trading_day(monkeypatch: pytest.MonkeyPatch):
    import src.data.calendar as calendar

    monkeypatch.setattr(calendar, "is_trading_day", lambda _d: True)
    now = dt.datetime(2026, 10, 30, 15, 30, tzinfo=tk._CN_TZ)
    assert tk.session_is_complete(now) is True
    midday = dt.datetime(2026, 10, 30, 11, 0, tzinfo=tk._CN_TZ)
    assert tk.session_is_complete(midday) is False


def test_session_is_complete_on_weekend(monkeypatch: pytest.MonkeyPatch):
    import src.data.calendar as calendar

    monkeypatch.setattr(calendar, "is_trading_day", lambda _d: False)
    saturday_morning = dt.datetime(2026, 10, 31, 9, 0, tzinfo=tk._CN_TZ)
    # The feed still holds Friday's finished session, so this must not block.
    assert tk.session_is_complete(saturday_morning) is True


def test_today_session_uses_beijing_date():
    stamp = dt.datetime(2026, 10, 30, 23, 0, tzinfo=dt.timezone.utc)
    assert tk.today_session(stamp) == "2026-10-31"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    yield tmp_path


def test_save_and_load_roundtrip(lake: Path):
    frame = _ticks([("09:30:01", 10.0, 100, "B"), ("09:30:02", 10.0, 100, "S")])
    row = tk.aggregate(frame, "600519.SH")
    assert tk.save([row]) == 1
    # Re-saving the same (symbol, date) must replace, not duplicate.
    assert tk.save([row]) == 1

    stored = tk.load(start=_TRADE_DATE, end=_TRADE_DATE)
    assert len(stored) == 1
    assert stored.iloc[0]["symbol"] == "600519.SH"
    assert stored.iloc[0]["active_buy_ratio"] == pytest.approx(0.5)


def test_save_ignores_empty_rows(lake: Path):
    assert tk.save([{}]) == 0
    assert tk.load().empty


def test_coverage_reports_sessions(lake: Path):
    frame = _ticks([("09:30:01", 10.0, 100, "B")])
    tk.save([tk.aggregate(frame, "600519.SH")])
    stored = tk.coverage()
    assert stored.iloc[0]["symbol"] == "600519.SH"
    assert int(stored.iloc[0]["sessions"]) == 1
