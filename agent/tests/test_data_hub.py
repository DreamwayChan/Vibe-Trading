"""Hub tests: one answer to "which session" and "is this dataset healthy".

These pin the two rules that stop screens from disagreeing: the settlement
cut-off (15:30) decides the default ``as_of`` for every consumer, and freshness
is measured against that same session rather than against "now".
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.data import catalog, hub
from src.data.registry import spec


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Isolated, offline lake so no test reaches the network for a calendar."""
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
    # ``EnvConfig`` is a cached singleton that outranks the env var inside
    # ``lake_root()`` — without this reset the lake stays pinned to whichever
    # directory an earlier test used, and writes land outside the tmp dir.
    reset_env_config()
    from src.data import calendar

    calendar.clear_cache()
    yield tmp_path
    reset_env_config()
    calendar.clear_cache()


def _at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=hub.CN_TZ)


def _manifest(name: str, *, rows: int, date_max: str) -> None:
    catalog.write_manifest(
        name,
        {
            "dataset": name,
            "schema_version": catalog.SCHEMA_VERSION,
            "rows": rows,
            "symbols": 1,
            "date_min": "2020-01-02",
            "date_max": date_max,
            "files": 1,
            "bytes": 1024,
            "partitions": [],
            "quality": {"dup_keys": 0, "checks": []},
        },
    )


def test_after_settlement_today_is_the_as_of(lake):
    result = hub.resolve_as_of(at=_at("2024-01-02T16:00"))

    assert result.date == "2024-01-02"
    assert result.settled is True


def test_during_session_defaults_to_the_previous_session(lake):
    result = hub.resolve_as_of(at=_at("2024-01-03T10:30"))

    assert result.date == "2024-01-02"
    assert result.settled is True
    assert result.live is False


def test_during_session_live_returns_today_unsettled(lake):
    result = hub.resolve_as_of(live=True, at=_at("2024-01-03T10:30"))

    assert result.date == "2024-01-03"
    assert result.settled is False
    assert result.live is True


def test_weekend_rolls_back_to_the_previous_session(lake):
    result = hub.resolve_as_of(at=_at("2024-01-06T11:00"))

    assert result.date == "2024-01-05"
    assert result.settled is True


def test_health_is_empty_without_data(lake):
    health = hub.dataset_health(spec("bars_daily"), as_of=hub.AsOf("2024-01-02", True, "t"))

    assert health["status"] == "empty"
    assert health["lag_days"] is None


def test_health_ok_when_caught_up(lake):
    _manifest("bars_daily", rows=10, date_max="2024-01-02")

    health = hub.dataset_health(
        spec("bars_daily"), as_of=hub.AsOf("2024-01-02", True, "t"), moment=_at("2024-01-02T16:00")
    )

    assert health["status"] == "ok"
    assert health["lag_days"] == 0


def test_health_pending_inside_the_sla_window(lake):
    """15:10 with yesterday's bars is not a defect yet — the SLA expires 15:40."""
    _manifest("bars_daily", rows=10, date_max="2024-01-02")

    health = hub.dataset_health(
        spec("bars_daily"), as_of=hub.AsOf("2024-01-03", True, "t"), moment=_at("2024-01-03T15:10")
    )

    assert health["status"] == "pending"
    assert health["lag_days"] == 1


def test_health_lags_after_the_sla_window(lake):
    _manifest("bars_daily", rows=10, date_max="2024-01-02")

    health = hub.dataset_health(
        spec("bars_daily"), as_of=hub.AsOf("2024-01-03", True, "t"), moment=_at("2024-01-03T16:00")
    )

    assert health["status"] == "lag"


def test_health_goes_stale_after_more_than_one_session(lake):
    _manifest("bars_daily", rows=10, date_max="2024-01-02")

    health = hub.dataset_health(
        spec("bars_daily"), as_of=hub.AsOf("2024-01-10", True, "t"), moment=_at("2024-01-10T16:00")
    )

    assert health["status"] == "stale"
    assert health["lag_days"] >= 2


def test_overview_carries_as_of_and_every_declared_dataset(lake):
    _manifest("bars_daily", rows=10, date_max="2024-01-02")

    payload = hub.overview()

    assert payload["as_of"]["date"]
    assert payload["counts"]["declared"] == len(payload["freshness"])
    assert payload["counts"]["with_data"] == 1
    assert payload["counts"]["total_rows"] == 10
    assert any(item["name"] == "bars_daily" for item in payload["freshness"])


def test_alerts_report_an_empty_dataset(lake):
    alerts = hub.quality_alerts(as_of=hub.AsOf("2024-01-02", True, "t"))

    codes = {(a["dataset"], a["code"]) for a in alerts}
    assert ("bars_daily", "empty") in codes


def test_alerts_surface_quality_violations(lake):
    catalog.write_manifest(
        "tradability",
        {
            "dataset": "tradability",
            "rows": 5,
            "date_max": "2024-01-02",
            "quality": {"dup_keys": 2, "checks": [{"id": "band_mismatch", "violations": 3}]},
        },
    )

    alerts = hub.quality_alerts(as_of=hub.AsOf("2024-01-02", True, "t"))
    messages = " ".join(a["message"] for a in alerts)

    assert "重复主键" in messages
    assert "band_mismatch" in messages


def test_strict_mode_reads_the_env_flag(lake, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_DATAHUB_STRICT", "1")
    assert hub.strict_mode() is True

    monkeypatch.setenv("VIBE_TRADING_DATAHUB_STRICT", "0")
    assert hub.strict_mode() is False


def test_lake_stats_counts_only_parquet(lake):
    import pandas as pd

    _ = pd  # keep the import local to the test that needs a frame
    from src.data import bars as bar_store

    index = pd.DatetimeIndex(pd.date_range("2024-01-02 09:35", "2024-01-02 15:00", freq="30min"))
    frame = pd.DataFrame(
        {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 1000.0}, index=index
    )
    bar_store.save_bars({"600519.SH": frame}, "30m")

    stats = hub.lake_stats()

    assert stats["files"] == 1
    assert stats["bytes"] > 0


# ---------------------------------------------------------------------------
# Series reads: bars + coverage grid
# ---------------------------------------------------------------------------


def _bars_frame(day: str, *, count: int = 6, base: float = 10.0):
    import pandas as pd

    stamps = pd.date_range(f"{day} 09:35", periods=count, freq="5min")
    return pd.DataFrame(
        {
            "open": [base] * count,
            "high": [base + 0.5] * count,
            "low": [base - 0.5] * count,
            "close": [base + 0.2] * count,
            "volume": [1000.0] * count,
        },
        index=stamps,
    )


def test_bars_returns_the_envelope_with_the_data(lake):
    from src.data import bars as bar_store

    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-02")}, "5m", source="tencent")

    payload = hub.bars("5m", symbols=["600519.SH"])

    assert payload["interval"] == "5m"
    assert payload["adjust"] == "none"
    assert payload["sources"] == ["tencent"]
    assert payload["coverage"]["symbols"] == 1
    assert payload["coverage"]["bars"] == 6
    assert payload["as_of"]["date"]  # every read says which session it answers for
    point = payload["series"]["600519.SH"]["points"][0]
    assert point["t"] == "2024-01-02 09:35"
    assert point["c"] == pytest.approx(10.2)


def test_bars_caps_points_and_says_so(lake):
    from src.data import bars as bar_store

    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-02", count=20)}, "5m")

    payload = hub.bars("5m", limit=5)

    assert payload["series"]["600519.SH"]["bars"] == 5
    # Truncation is reported, never silent: the caller can label the chart.
    assert any("截断" in warning for warning in payload["warnings"])
    # ...and the cap keeps the most recent bars, not the oldest.
    assert payload["series"]["600519.SH"]["points"][-1]["t"] == "2024-01-02 11:10"


def test_bars_on_an_empty_window_explains_itself(lake):
    payload = hub.bars("1d", symbols=["600519.SH"])

    assert payload["series"] == {}
    assert payload["warnings"] == ["湖内没有该周期与区间的数据"]


def test_coverage_grid_counts_gaps_against_the_calendar(lake):
    from src.data import bars as bar_store

    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-02")}, "5m")
    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-05")}, "5m")

    grid = hub.coverage_grid("5m", start="2024-01-02", end="2024-01-05")

    keys = [bucket["key"] for bucket in grid["buckets"]]
    # 01-03 and 01-04 have no data but are trading days: they must still be columns.
    assert keys == ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    covered = {(cell["bucket"]): cell["covered"] for cell in grid["cells"]}
    assert covered["2024-01-02"] == 1
    assert covered["2024-01-03"] == 0


def test_coverage_grid_buckets_by_month_when_the_span_is_long(lake):
    from src.data import bars as bar_store

    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-02")}, "5m")
    bar_store.save_bars({"600519.SH": _bars_frame("2024-12-31")}, "5m")

    grid = hub.coverage_grid("5m")

    assert grid["granularity"] == "month"
    assert [bucket["key"] for bucket in grid["buckets"]][0] == "2024-01"
    assert grid["buckets"][0]["sessions"] >= 20  # January's trading sessions


# ---------------------------------------------------------------------------
# Adjusted views
# ---------------------------------------------------------------------------
#
# The lake keeps unadjusted prices plus a cumulative factor, so "what did this
# return" depends entirely on one conversion.  These pin that conversion: the
# latest bar keeps its raw price under qfq, the ex-dividend day stops reading as
# a loss, and a bar without a factor is dropped rather than silently treated as
# dividend-free.


def _seed_daily_with_factor(symbol: str = "600519.SH", factors=(2.0, 2.0, 4.0)):
    import pandas as pd

    from src.data import bars as bar_store

    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    closes = [10.0, 11.0, 5.6]
    frame = pd.DataFrame(
        {
            "symbol": [symbol] * len(days),
            "date": pd.to_datetime(days),
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(days),
            "pre_close": [float("nan")] + closes[:-1],
            "adj_factor": list(factors),
        }
    )
    bar_store.save_long_bars(frame, "1d", source="sina")


def test_adjusted_bars_qfq_keeps_the_latest_price_raw(lake):
    _seed_daily_with_factor()

    adjusted = hub.adjusted_bars("1d", symbols=["600519.SH"], mode="qfq")

    # factor 4.0 is the latest, so the last bar is untouched and the earlier ones
    # are halved — the 2024-01-04 ex-dividend drop (11 → 5.6) becomes 5.5 → 5.6.
    assert adjusted["close"].round(4).tolist() == [5.0, 5.5, 5.6]
    assert adjusted["high"].round(4).tolist() == [5.1, 5.6, 5.8]
    # Volume is a share count: a dividend does not change it.
    assert adjusted["volume"].tolist() == [1000.0] * 3
    assert adjusted.attrs["adjust"] == "qfq"
    assert adjusted.attrs["missing_factor"] == 0


def test_adjusted_bars_hfq_multiplies_by_the_stored_factor(lake):
    _seed_daily_with_factor()

    adjusted = hub.adjusted_bars("1d", symbols=["600519.SH"], mode="hfq")

    assert adjusted["close"].round(4).tolist() == [20.0, 22.0, 22.4]


def test_adjusted_bars_recomputes_pct_chg_on_the_adjusted_series(lake):
    _seed_daily_with_factor()

    adjusted = hub.adjusted_bars("1d", symbols=["600519.SH"], mode="qfq")

    # Raw 11 → 5.6 is -49% because the dividend left the price; adjusted it is
    # +1.8%.  The stored pct_chg would have said -49.
    assert round(float(adjusted["pct_chg"].iloc[-1]), 4) == round((5.6 / 5.5 - 1) * 100, 4)
    assert adjusted["pct_chg"].iloc[0] != adjusted["pct_chg"].iloc[0]  # NaN, no prior bar


def test_adjusted_bars_drops_rows_without_a_factor_and_counts_them(lake):
    _seed_daily_with_factor(factors=(2.0, float("nan"), 4.0))

    adjusted = hub.adjusted_bars("1d", symbols=["600519.SH"], mode="qfq")

    assert len(adjusted) == 2
    assert adjusted.attrs["missing_factor"] == 1


def test_adjusted_bars_rejects_an_unknown_mode(lake):
    _seed_daily_with_factor()

    with pytest.raises(ValueError):
        hub.adjusted_bars("1d", symbols=["600519.SH"], mode="split")


def test_bars_envelope_labels_the_adjustment_it_applied(lake):
    _seed_daily_with_factor()

    payload = hub.bars("1d", symbols=["600519.SH"], adjust="qfq")

    assert payload["adjust"] == "qfq"
    closes = [point["c"] for point in payload["series"]["600519.SH"]["points"]]
    assert closes == [5.0, 5.5, 5.6]
    assert payload["coverage"]["bars"] == 3


def test_bars_warns_when_the_adjusted_view_drops_bars(lake):
    _seed_daily_with_factor(factors=(2.0, float("nan"), 4.0))

    payload = hub.bars("1d", symbols=["600519.SH"], adjust="qfq")

    assert payload["coverage"]["bars"] == 2
    assert any("没有复权因子" in warning for warning in payload["warnings"])


def test_bars_on_an_intraday_series_refuses_to_adjust(lake):
    """No daily factor anywhere → the adjusted view cannot be defined.

    Intraday bars inherit their session's factor (see
    ``test_intraday_bars_inherit_their_session_factor``), so this is the case
    where the daily series is missing too — saying so beats returning raw prices
    under an "qfq" label.
    """
    from src.data import bars as bar_store

    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-02")}, "5m")

    with pytest.raises(ValueError):
        hub.bars("5m", symbols=["600519.SH"], adjust="qfq")


def _seed_minutes_across_a_dividend() -> None:
    """Two sessions of 5-minute bars, the second one ex-dividend.

    2024-01-03 pays a dividend: the raw price drops 10.0 → 9.6 while the
    cumulative factor steps 1.0 → 1.04, so the *adjusted* series is flat.  That
    is exactly the case a minute-level study must not see as a -4% crash.
    """
    import pandas as pd

    from src.data import bars as bar_store

    daily = pd.DataFrame(
        {
            "symbol": ["600519.SH"] * 2,
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [10.0, 9.6],
            "high": [10.2, 9.8],
            "low": [9.8, 9.4],
            "close": [10.0, 9.6],
            "volume": [1000.0, 1000.0],
            "adj_factor": [1.0, 1.04],
        }
    )
    bar_store.save_long_bars(daily, "1d", source="test")

    for day, price in (("2024-01-02", 10.0), ("2024-01-03", 9.6)):
        frame = _bars_frame(day, base=price)
        bar_store.save_bars({"600519.SH": frame}, "5m", source="test")


def test_intraday_bars_inherit_their_session_factor(lake):
    """A minute bar has no factor; the session's factor is the honest answer."""
    _seed_minutes_across_a_dividend()

    payload = hub.bars("5m", symbols=["600519.SH"], adjust="qfq")

    points = payload["series"]["600519.SH"]["points"]
    first = next(p for p in points if p["t"].startswith("2024-01-02"))
    second = next(p for p in points if p["t"].startswith("2024-01-03"))
    # qfq scales the older session down by its factor ratio and leaves the newest
    # at its printed price, so the ex-dividend session stops looking like -4%.
    assert first["c"] == pytest.approx(10.2 / 1.04, abs=1e-4)
    assert second["c"] == pytest.approx(9.8, abs=1e-4)
    assert payload["coverage"]["bars"] == 12


def test_intraday_hfq_multiplies_by_the_cumulative_factor(lake):
    _seed_minutes_across_a_dividend()

    frame = hub.adjusted_bars("5m", symbols=["600519.SH"], mode="hfq").sort_values("date")

    first = frame[frame["date"].dt.strftime("%Y-%m-%d") == "2024-01-02"]
    second = frame[frame["date"].dt.strftime("%Y-%m-%d") == "2024-01-03"]
    # `_bars_frame` prints close = base + 0.2, so the raw closes are 10.2 / 9.8.
    assert first["close"].iloc[0] == pytest.approx(10.2 * 1.0)
    assert second["close"].iloc[0] == pytest.approx(9.8 * 1.04)


def test_intraday_factor_falls_back_to_the_previous_session(lake):
    """A bar whose session has no daily row still gets the factor in force.

    This is the resumption / newly-listed case: the daily series starts a day
    later than the minute feed, and "no factor" would silently drop the bars.
    """
    import pandas as pd

    from src.data import bars as bar_store

    daily = pd.DataFrame(
        {
            "symbol": ["600519.SH"],
            "date": pd.to_datetime(["2024-01-02"]),
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.0],
            "volume": [1000.0],
            "adj_factor": [2.0],
        }
    )
    bar_store.save_long_bars(daily, "1d", source="test")
    bar_store.save_bars({"600519.SH": _bars_frame("2024-01-03", base=10.0)}, "5m", source="test")

    frame = hub.adjusted_bars("5m", symbols=["600519.SH"], mode="hfq")

    assert len(frame) == 6
    assert frame["adj_factor"].unique().tolist() == [2.0]


# ---------------------------------------------------------------------------
# Raw frames: what a screener reads instead of the chart envelope
# ---------------------------------------------------------------------------


def _daily_frame(*days: str, base: float = 10.0):
    import pandas as pd

    return pd.DataFrame(
        {
            "open": [base] * len(days),
            "high": [base + 0.5] * len(days),
            "low": [base - 0.5] * len(days),
            "close": [base + 0.1] * len(days),
            "volume": [1000.0] * len(days),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


def test_bars_frame_splits_symbols_and_keeps_the_last_source(lake):
    from src.data import bars as bar_store

    bar_store.save_bars(
        {"600519.SH": _daily_frame("2024-01-02"), "000001.SZ": _daily_frame("2024-01-02")},
        "1d",
        source="tushare",
    )

    stored = hub.bars_frame(
        "1D", symbols=["600519.SH", "000001.SZ"], start="2024-01-01", end="2024-01-05"
    )

    assert bool(stored) is True
    assert set(stored.frames) == {"600519.SH", "000001.SZ"}
    # Raw frames, not the rounded JSON envelope: a screener needs the numbers.
    assert stored.frames["600519.SH"]["close"].iloc[0] == pytest.approx(10.1)
    assert stored.sources["600519.SH"] == "tushare"
    assert stored.derived_from is None


def test_bars_frame_filters_to_the_requested_window(lake):
    from src.data import bars as bar_store

    bar_store.save_bars(
        {"600519.SH": _daily_frame("2024-01-02", "2024-01-03", "2024-01-04")},
        "1d",
        source="tushare",
    )

    stored = hub.bars_frame("1D", symbols=["600519.SH"], start="2024-01-03", end="2024-01-03")

    assert len(stored.frames["600519.SH"]) == 1


def test_bars_frame_on_an_empty_lake_is_falsy(lake):
    stored = hub.bars_frame("1D", symbols=["600519.SH"])

    assert bool(stored) is False
    assert stored.frames == {}
    assert stored.sources == {}


# ---------------------------------------------------------------------------
# Raw frames: what a screener reads instead of the chart envelope
# ---------------------------------------------------------------------------


def _daily_frame(*days: str, base: float = 10.0):
    import pandas as pd

    return pd.DataFrame(
        {
            "open": [base] * len(days),
            "high": [base + 0.5] * len(days),
            "low": [base - 0.5] * len(days),
            "close": [base + 0.1] * len(days),
            "volume": [1000.0] * len(days),
        },
        index=pd.DatetimeIndex([pd.Timestamp(day) for day in days]),
    )


def test_bars_frame_splits_symbols_and_keeps_the_last_source(lake):
    from src.data import bars as bar_store

    bar_store.save_bars(
        {"600519.SH": _daily_frame("2024-01-02"), "000001.SZ": _daily_frame("2024-01-02")},
        "1d",
        source="tushare",
    )

    stored = hub.bars_frame(
        "1D", symbols=["600519.SH", "000001.SZ"], start="2024-01-01", end="2024-01-05"
    )

    assert bool(stored) is True
    assert set(stored.frames) == {"600519.SH", "000001.SZ"}
    # Raw frames, not the rounded JSON envelope: a screener needs the numbers.
    assert stored.frames["600519.SH"]["close"].iloc[0] == pytest.approx(10.1)
    assert stored.sources["600519.SH"] == "tushare"
    assert stored.derived_from is None


def test_bars_frame_filters_to_the_requested_window(lake):
    from src.data import bars as bar_store

    bar_store.save_bars(
        {"600519.SH": _daily_frame("2024-01-02", "2024-01-03", "2024-01-04")},
        "1d",
        source="tushare",
    )

    stored = hub.bars_frame("1D", symbols=["600519.SH"], start="2024-01-03", end="2024-01-03")

    assert len(stored.frames["600519.SH"]) == 1


def test_bars_frame_on_an_empty_lake_is_falsy(lake):
    stored = hub.bars_frame("1D", symbols=["600519.SH"])

    assert bool(stored) is False
    assert stored.frames == {}
    assert stored.sources == {}


def test_bars_frame_drops_bars_that_violate_the_ohlc_contract(lake):
    """A screener reads frames straight from here, so validation is not optional."""
    import pandas as pd

    from src.data import bars as bar_store

    dirty = _daily_frame("2024-01-02", "2024-01-03", "2024-01-04")
    dirty.loc[pd.Timestamp("2024-01-03"), "high"] = 1.0  # high < low
    bar_store.save_bars({"600519.SH": dirty}, "1d", source="tushare")

    stored = hub.bars_frame(
        "1D", symbols=["600519.SH"], start="2024-01-01", end="2024-01-05"
    )

    frame = stored.frames["600519.SH"]
    assert pd.Timestamp("2024-01-03") not in frame.index
    assert len(frame) == 2
