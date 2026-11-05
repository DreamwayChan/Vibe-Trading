"""Lake-first fetching: what a scan reads, and what it has to go out for."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.hub import StoredBars
from src.data.lake_fetch import FetchStats, fetch_lake_first


def _daily(*days: str, base: float = 10.0) -> pd.DataFrame:
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


@pytest.fixture
def lake_bars(monkeypatch: pytest.MonkeyPatch):
    """Serve ``hub.bars_frame`` from memory: no parquet, no lake root, no I/O."""

    def install(frames: dict[str, pd.DataFrame], sources: dict[str, str] | None = None):
        import src.data.hub as hub

        stored = StoredBars(
            interval="1D", frames=frames, sources=sources or {}, derived_from=None
        )
        monkeypatch.setattr(hub, "bars_frame", lambda *a, **k: stored)

    return install


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch):
    """Network stub that records (start, end, source, symbols) and returns nothing."""

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, tuple[str, ...]]] = []
            self.answer: dict[str, pd.DataFrame] = {}

        def __call__(self, symbols, *, start, end, source, interval):
            self.calls.append((start, end, source, tuple(symbols)))
            frames = {s: self.answer[s] for s in symbols if s in self.answer}
            return frames, {s: "no OHLCV bars returned" for s in symbols if s not in frames}

    recorder = Recorder()
    return recorder


def _run(symbols, *, start, end, source, network, stats=None):
    return fetch_lake_first(
        symbols,
        start=start,
        end=end,
        source=source,
        interval="1D",
        network=network,
        label="test",
        stats=stats,
    )


def test_label_names_the_cheapest_source_that_covered_the_scan() -> None:
    assert FetchStats().label() is None
    assert FetchStats(requested=10, lake_bases=10).label() == "lake"
    # Any bar pulled from a vendor is not "data centre", even when the stored
    # frames covered the rest: the badge is read as "nothing came off the wire".
    assert FetchStats(requested=10, lake_bases=9, pulled_whole=1, network_calls=1).label() == "lake+network"
    assert FetchStats(requested=10, lake_bases=10, tail_spliced=3, network_calls=1).label() == "lake+network"
    assert FetchStats(requested=10, pulled_whole=10, network_calls=1).label() == "network"


def test_a_handful_of_full_pulls_does_not_masquerade_as_a_local_scan(
    lake_bars, net
) -> None:
    """One name needing the vendor used to leave the badge saying "data centre"."""
    lake_bars(
        {"AAA.SZ": _daily("2024-01-02", "2024-01-03")}, {"AAA.SZ": "tushare"}
    )
    net.answer["NEW.SZ"] = _daily("2024-01-02", "2024-01-03")

    _, _, stats = _run(
        ["AAA.SZ", "NEW.SZ"], start="2024-01-01", end="2024-01-03",
        source="tushare", network=net,
    )

    assert stats.pulled_whole == 1
    assert stats.label() == "lake+network"


def test_a_vendor_failure_keeps_the_stored_frames(lake_bars) -> None:
    """A 500 on the tail must not blank a screen the lake already covers."""

    def exploding(symbols, *, start, end, source, interval):
        raise RuntimeError("tushare 502")

    lake_bars(
        {"AAA.SZ": _daily("2024-01-02", "2024-01-03")}, {"AAA.SZ": "tushare"}
    )

    data, errors, stats = _run(
        ["AAA.SZ"], start="2024-01-01", end="2024-01-05",
        source="tushare", network=exploding,
    )

    assert len(data["AAA.SZ"]) == 2
    assert "502" in errors["AAA.SZ"]
    assert stats.label() == "lake+network"


def test_suspended_names_do_not_each_become_their_own_request(lake_bars, net) -> None:
    """Thirty names that stopped trading a year ago used to cost thirty calls."""
    frames = {}
    for index in range(40):
        frames[f"ACTIVE{index}.SZ"] = _daily("2024-01-02", "2024-01-03")
    for index in range(30):
        frames[f"SUS{index}.SZ"] = _daily(f"2023-{1 + index % 6:02d}-{1 + index % 9:02d}")
    lake_bars(frames, {symbol: "tushare" for symbol in frames})

    data, errors, stats = _run(
        list(frames), start="2024-01-01", end="2024-01-03", source="tushare", network=net
    )

    assert net.calls == []
    assert stats.stale_kept == 30
    assert set(data) == set(frames)
    assert errors == {}
    assert stats.label() == "lake"


def test_a_uniformly_stale_lake_is_still_topped_up(lake_bars, net) -> None:
    """When ingestion itself lags, every name shares the stale date — fetch them."""
    from src.monitor.trend_regime import CSI300

    frames = {f"S{index}.SZ": _daily("2023-11-02", "2023-12-01") for index in range(60)}
    lake_bars(frames, {symbol: "tushare" for symbol in frames})
    net.answer = {symbol: _daily("2024-01-03") for symbol in frames}
    net.answer[CSI300] = _daily("2024-01-03")

    _, _, stats = _run(
        list(frames), start="2023-11-01", end="2024-01-03", source="tushare", network=net
    )

    assert stats.stale_kept == 0
    # Probe plus one bucket: the whole universe shares the same gap.
    assert len(net.calls) == 2
    assert len(net.calls[1][3]) == 60
    assert stats.tail_spliced == 60


def test_a_failed_probe_is_not_read_as_a_holiday(lake_bars) -> None:
    """A broken index request must not declare a stale universe current."""
    from src.monitor.trend_regime import CSI300

    frames = {f"S{index}.SZ": _daily("2024-01-02") for index in range(60)}
    lake_bars(frames, {symbol: "tushare" for symbol in frames})
    calls: list[tuple[str, tuple[str, ...]]] = []

    def half_broken(symbols, *, start, end, source, interval):
        calls.append((start, tuple(symbols)))
        if tuple(symbols) == (CSI300,):
            return {}, {CSI300: "tushare: token expired"}
        return {symbol: _daily("2024-01-03") for symbol in symbols}, {}

    _, errors, stats = _run(
        list(frames), start="2024-01-01", end="2024-01-03",
        source="tushare", network=half_broken,
    )

    assert calls[0][1] == (CSI300,)
    # Probe failed for a reason other than "no session", so the gap is fetched.
    assert len(calls) == 2
    assert stats.tail_spliced == 60
    assert errors == {}


def test_stored_window_never_touches_the_network(lake_bars, net) -> None:
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03", "2024-01-04")},
        {"600519.SH": "tushare"},
    )

    data, errors, stats = _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04", source="tushare", network=net
    )

    assert net.calls == []
    assert list(data) == ["600519.SH"]
    assert errors == {}
    assert stats.label() == "lake"


def test_auto_follows_the_lake_source_so_the_seam_stays_one_series(lake_bars, net) -> None:
    """``auto`` would route the tail to whichever vendor answers first."""
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tushare"}
    )
    net.answer["600519.SH"] = _daily("2024-01-04")

    data, _, stats = _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04", source="auto", network=net
    )

    # Same vendor that wrote the lake, not whatever auto would have picked.
    assert net.calls == [("2024-01-04", "2024-01-04", "tushare", ("600519.SH",))]
    assert len(data["600519.SH"]) == 3
    assert stats.label() == "lake+network"


def test_explicit_source_mismatch_refetches_instead_of_splicing(lake_bars, net) -> None:
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tencent"}
    )
    net.answer["600519.SH"] = _daily("2024-01-02", "2024-01-03", "2024-01-04")

    _run(["600519.SH"], start="2024-01-01", end="2024-01-04", source="tushare", network=net)

    assert net.calls == [("2024-01-01", "2024-01-04", "tushare", ("600519.SH",))]


def test_unusable_provenance_is_pulled_whole(lake_bars, net) -> None:
    """A frame the lake tagged ``local`` has no vendor to top it up from."""
    lake_bars({"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "local"})
    net.answer["600519.SH"] = _daily("2024-01-02", "2024-01-03", "2024-01-04")

    _run(["600519.SH"], start="2024-01-01", end="2024-01-04", source="auto", network=net)

    assert net.calls == [("2024-01-01", "2024-01-04", "auto", ("600519.SH",))]


def test_holiday_gap_costs_one_probe_not_a_fanout(lake_bars, net) -> None:
    symbols = [f"60{index:04d}.SH" for index in range(60)]
    lake_bars(
        {symbol: _daily("2024-01-02", "2024-01-03") for symbol in symbols},
        {symbol: "tushare" for symbol in symbols},
    )

    data, errors, stats = _run(
        symbols, start="2024-01-01", end="2024-01-08", source="tushare", network=net
    )

    assert net.calls == [("2024-01-04", "2024-01-08", "tushare", ("000300.SH",))]
    assert stats.probe_skipped is True
    assert len(data) == 60
    assert errors == {}


def test_suspended_name_keeps_its_frame_and_is_not_an_error(lake_bars, net) -> None:
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tushare"}
    )

    data, errors, _ = _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04", source="tushare", network=net
    )

    assert len(data["600519.SH"]) == 2
    assert errors == {}


def test_empty_lake_falls_back_to_one_full_pull(net) -> None:
    _, _, stats = _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04", source="tushare", network=net
    )

    assert net.calls == [("2024-01-01", "2024-01-04", "tushare", ("600519.SH",))]
    assert stats.label() == "network"


def test_a_tushare_refresh_is_served_from_the_lake_on_the_next_call(net) -> None:
    """The gap is fetched once, stored, and the following scan does not repeat it."""
    frame = _daily("2024-01-02", "2024-01-03", "2024-01-04")
    frame["amount"] = 2.5
    net.answer["600519.SH"] = frame

    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tushare", network=net,
    )
    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tushare", network=net,
    )

    assert net.calls == [("2024-01-01", "2024-01-04", "tushare", ("600519.SH",))]

    from src.data.bars import load_bars

    stored = load_bars("1d", symbols=["600519.SH"])
    assert len(stored) == 3
    assert stored["amount"].iloc[0] == pytest.approx(2500.0)
    assert set(stored["source"]) == {"tushare"}


def test_tencent_bars_are_not_written_over_the_unadjusted_lake(net) -> None:
    """The Tencent loader is forward-adjusted, so a refresh must not be cached."""
    net.answer["600519.SH"] = _daily("2024-01-02", "2024-01-03", "2024-01-04")

    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tencent", network=net,
    )
    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tencent", network=net,
    )

    assert len(net.calls) == 2


def test_a_fallback_marked_skip_is_not_cached(net) -> None:
    frame = _daily("2024-01-02", "2024-01-03", "2024-01-04")
    frame.attrs["data_center_source"] = "skip"
    net.answer["600519.SH"] = frame

    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tushare", network=net,
    )
    _run(
        ["600519.SH"], start="2024-01-01", end="2024-01-04",
        source="tushare", network=net,
    )

    assert len(net.calls) == 2


def test_the_forming_session_is_served_but_not_stored(net) -> None:
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone(timedelta(hours=8))).date()
    yesterday = today - timedelta(days=1)
    net.answer["600519.SH"] = _daily(yesterday.isoformat(), today.isoformat())

    _run(
        ["600519.SH"], start=yesterday.isoformat(), end=today.isoformat(),
        source="tushare", network=net,
    )

    from src.data.bars import load_bars

    stored = load_bars("1d", symbols=["600519.SH"])
    stored_days = set(pd.to_datetime(stored["date"]).dt.date)
    assert yesterday in stored_days
    assert today not in stored_days

    _run(
        ["600519.SH"], start=yesterday.isoformat(), end=today.isoformat(),
        source="tushare", network=net,
    )
    # Yesterday is cached; the still-forming session is asked for again.
    assert len(net.calls) == 2
    assert net.calls[1][0] == today.isoformat()


def test_strict_mode_serves_the_lake_and_does_not_refresh(
    lake_bars, net, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATAHUB_STRICT", "1")
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")},
        {"600519.SH": "tushare"},
    )

    data, errors, stats = _run(
        ["600519.SH", "000001.SZ"], start="2024-01-01", end="2024-01-05",
        source="tushare", network=net,
    )

    assert net.calls == []
    assert len(data["600519.SH"]) == 2
    assert "strict" in errors["000001.SZ"]
    assert stats.network_calls == 0


def test_backtest_fetch_uses_a_fresh_lake_without_calling_the_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backtest.runner import fetch_data_map
    from src.data.bars import save_bars

    save_bars(
        {"000001.SZ": _daily("2024-01-02", "2024-01-03", "2024-01-04")},
        "1d",
        source="tushare",
    )

    def explode(source: str):
        raise AssertionError(f"loader {source} should not run")

    monkeypatch.setattr("backtest.runner._get_loader", explode)

    result = fetch_data_map(
        {
            "codes": ["000001.SZ"],
            "start_date": "2024-01-01",
            "end_date": "2024-01-04",
            "source": "tushare",
            "interval": "1D",
        }
    )

    assert list(result.data_map) == ["000001.SZ"]
    assert len(result.data_map["000001.SZ"]) == 3
    assert result.effective_sources == ["lake"]


def test_sector_pick_reads_the_shared_lake(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.data.bars import save_bars
    from src.sector_pick.scan import _fetch_bars

    save_bars(
        {"000001.SZ": _daily("2024-01-02", "2024-01-03", "2024-01-04")},
        "1d",
        source="tushare",
    )
    calls: list[tuple[str, ...]] = []

    def fake(symbols, *, start, end, source, interval):
        calls.append(tuple(symbols))
        return {}, {symbol: "no OHLCV bars returned" for symbol in symbols}

    monkeypatch.setattr("src.sector_pick.scan._fetch_universe_ohlcv", fake)

    data, errors = _fetch_bars(
        ["000001.SZ"],
        start="2024-01-01",
        end="2024-01-04",
        source="tushare",
        interval="1D",
    )

    assert calls == []
    assert errors == {}
    assert len(data["000001.SZ"]) == 3
