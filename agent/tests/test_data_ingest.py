"""Tests for the ingest layer: job policy, window resolution and bar writes.

These are offline by construction.  The point of the layer is that "what to
fetch" and "what happened" are decided before any network call, so every
property worth pinning — incremental window, single flight, idempotent upsert,
unit conversion, honest refusals — can be tested with a stubbed fetcher.
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from src.data import bars as bar_store
from src.data import ingest


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Temporary lake, offline calendar, clean job ledger."""
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_CALENDAR_OFFLINE", "1")
    # ``EnvConfig`` is a cached singleton and outranks the env var inside
    # ``lake_root()``; without this reset a lake fixed by an earlier test would
    # outlive its own tmp dir (and a background heartbeat could write to the
    # developer's real lake).
    reset_env_config()
    ingest._reset_for_tests()
    yield tmp_path
    ingest._reset_for_tests()
    reset_env_config()


@pytest.fixture
def sessions(monkeypatch):
    """A deterministic weekday calendar for the window logic."""

    def _days(start, end):
        span = pd.bdate_range(str(start), str(end))
        return [d.date().isoformat() for d in span]

    monkeypatch.setattr("src.data.calendar.trading_days", _days)
    monkeypatch.setattr("src.data.calendar.is_trading_day", lambda day: True)
    monkeypatch.setattr(
        "src.data.calendar.next_trading_day",
        lambda day, n=1: (pd.Timestamp(day) + pd.Timedelta(days=1)).date().isoformat(),
    )
    return _days


def _stub_tencent(rows_per_session: int = 1):
    """Replace the Tencent HTTP call with deterministic ascending rows."""

    def _fetch(symbol: str, start: str, end: str, **kwargs: object):
        span = pd.bdate_range(start, end)
        base = 10.0 if symbol.startswith("0") else 20.0
        out = []
        for index, day in enumerate(span):
            price = base + index
            out.append(
                [
                    day.date().isoformat(),
                    f"{price:.2f}",
                    f"{price + 0.5:.2f}",
                    f"{price + 1:.2f}",
                    f"{price - 1:.2f}",
                    str(1000 * rows_per_session),
                ]
            )
        return out

    return _fetch


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_every_declared_ingester_resolves():
    """A registry entry that names a missing function would 500 at click time."""
    from src.data.registry import REGISTRY

    for name, dataset_spec in REGISTRY.items():
        if not dataset_spec.ingest:
            continue
        resolved = ingest._callable_for(name)
        assert callable(resolved), name


def test_dataset_options_marks_every_dataset(lake):
    options = ingest.dataset_options()
    assert {item["name"] for item in options} == set(__import__("src.data.registry", fromlist=["REGISTRY"]).REGISTRY)
    assert all(item["runnable"] for item in options)
    bars = next(item for item in options if item["name"] == "bars_daily")
    assert "start" in bars["params"] and "symbols" in bars["params"]


def test_unknown_dataset_is_an_error(lake):
    with pytest.raises(ingest.IngestError):
        ingest.run("nope")


# ---------------------------------------------------------------------------
# Daily ingestion
# ---------------------------------------------------------------------------


def test_daily_writes_rows_and_reports_source(lake, sessions, monkeypatch):
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())

    payload = ingest.run(
        "bars_daily",
        start="2026-09-01",
        end="2026-09-11",
        symbols=["600519.SH", "000001.SZ"],
    )

    assert payload["status"] == "done", payload
    report = payload["report"]
    assert report["source"] == "tencent"
    assert report["symbols"] == 2
    assert report["rows"] == 2 * len(sessions("2026-09-01", "2026-09-11"))

    stored = bar_store.load_bars("1d", symbols=["600519.SH"])
    assert not stored.empty
    assert set(stored["source"]) == {"tencent"}
    # Units are part of the contract: 手 for volume, 元 for amount.
    assert stored["amount"].isna().all()
    assert stored["volume"].iloc[0] == 1000.0


def test_daily_rerun_is_idempotent(lake, sessions, monkeypatch):
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())
    args = dict(start="2026-09-01", end="2026-09-11", symbols=["600519.SH"])

    first = ingest.run("bars_daily", **args)
    second = ingest.run("bars_daily", **args)

    # The second run has nothing left to do: the symbol already holds the whole
    # window, so it is skipped rather than re-fetched and re-written.
    assert first["report"]["rows"] > 0
    assert second["report"]["rows"] == 0
    assert any("已覆盖" in note for note in second["report"]["notes"])

    stored = bar_store.load_bars("1d", symbols=["600519.SH"])
    assert len(stored) == first["report"]["rows"]
    assert stored["date"].is_unique


def test_daily_starts_after_the_watermark(lake, sessions, monkeypatch):
    """An existing watermark must turn a full backfill into an incremental one."""
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())
    ingest.run("bars_daily", start="2026-09-01", end="2026-09-04", symbols=["600519.SH"])

    payload = ingest.run("bars_daily", end="2026-09-08", symbols=["600519.SH"])

    assert payload["report"]["start"] == "2026-09-05"
    assert any("增量补齐" in note for note in payload["report"]["notes"])


def test_daily_reports_covered_window_without_fetching(lake, sessions, monkeypatch):
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())
    ingest.run("bars_daily", start="2026-09-01", end="2026-09-11", symbols=["600519.SH"])

    def _explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a covered window must not hit the network")

    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _explode)
    payload = ingest.run("bars_daily", end="2026-09-11", symbols=["600519.SH"])

    assert payload["status"] == "done"
    assert payload["report"]["rows"] == 0
    assert any("已覆盖" in note for note in payload["report"]["notes"])


def test_tushare_normalisation_converts_units_and_filters(lake):
    raw = pd.DataFrame(
        {
            "ts_code": ["600519.SH", "000001.SZ"],
            "trade_date": ["20260901", "20260901"],
            "open": [10.0, 20.0],
            "high": [11.0, 21.0],
            "low": [9.0, 19.0],
            "close": [10.5, 20.5],
            "pre_close": [10.0, 20.0],
            "pct_chg": [5.0, 2.5],
            "vol": [100.0, 200.0],
            "amount": [1.5, 2.5],  # 千元
        }
    )
    frame = ingest_bars()._normalize_tushare_daily(raw, {"600519.SH"})

    assert list(frame["symbol"]) == ["600519.SH"]
    assert frame["amount"].iloc[0] == pytest.approx(1500.0)
    assert frame["volume"].iloc[0] == 100.0
    assert frame["date"].iloc[0] == pd.Timestamp("2026-09-01")


def test_tencent_symbol_mapping():
    bars_module = ingest_bars()
    assert bars_module._tencent_code("600519.SH") == "sh600519"
    assert bars_module._tencent_code("000001.SZ") == "sz000001"
    assert bars_module._tencent_code("830799.BJ") is None
    assert bars_module._tencent_code("600519") is None


def ingest_bars():
    from src.data.ingest import bars as bars_module

    return bars_module


# ---------------------------------------------------------------------------
# Tradability
# ---------------------------------------------------------------------------


def test_tradability_refuses_without_daily_bars(lake, sessions):
    payload = ingest.run("tradability", start="2026-09-01", end="2026-09-02")

    assert payload["status"] == "done"
    assert payload["report"]["rows"] == 0
    assert any("bars_daily" in note for note in payload["report"]["notes"])


def test_tradability_builds_from_stored_daily_bars(lake, sessions, monkeypatch):
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())
    ingest.run("bars_daily", start="2026-09-01", end="2026-09-11", symbols=["600519.SH"])

    payload = ingest.run("tradability", start="2026-09-01", end="2026-09-11")

    assert payload["status"] == "done", payload
    report = payload["report"]
    assert report["rows"] == len(sessions("2026-09-01", "2026-09-11"))
    assert report["source"] == "bars_daily"

    from src.data import tradability

    panel = tradability.load_panel(start="2026-09-01", end="2026-09-11")
    assert not panel.empty
    assert panel["limit_up"].iloc[-1] > panel["pre_close"].iloc[-1]


# ---------------------------------------------------------------------------
# Job policy
# ---------------------------------------------------------------------------


def test_single_flight_rejects_a_second_job(lake, sessions, monkeypatch):
    import time

    import pandas as pd

    def _slow(*args, **kwargs):
        time.sleep(0.4)
        return []

    def _no_sina(*args, **kwargs):
        return pd.DataFrame()

    # An empty tencent answer sends the chain to sina *and then Eastmoney*, both
    # real HTTP. Left unstubbed the job outlived the 5s wait below and the
    # assertion depended on whatever the network did that day — the single-flight
    # rule was never the thing under test. Stub every fallback so the job ends on
    # its own schedule.
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _slow)
    monkeypatch.setattr("src.data.ingest.bars._fetch_sina_frame", _no_sina)
    monkeypatch.setattr(
        "src.data.ingest.bars._ingest_daily_eastmoney", lambda *args, **kwargs: ""
    )

    queued = ingest.submit("bars_daily", start="2026-09-01", end="2026-09-02", symbols=["600519.SH"])
    try:
        with pytest.raises(ingest.IngestBusy):
            ingest.run("bars_daily", start="2026-09-01", end="2026-09-02")
    finally:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not ingest.active():
                break
            time.sleep(0.05)

    assert queued["status"] in {"queued", "running", "done"}
    assert not ingest.active()


def test_failed_job_records_the_error(lake, sessions, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("source down")

    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _boom)

    payload = ingest.run(
        "bars_daily",
        start="2026-09-01",
        end="2026-09-02",
        symbols=["600519.SH"],
        prefer="tencent",  # pin the chain: the sina fallback would really fetch
    )

    # A per-symbol failure is data, not a job failure: nothing landed, so the
    # report says so instead of raising into the UI.
    assert payload["status"] == "done"
    assert payload["report"]["rows"] == 0
    assert not ingest.active()


def test_job_history_survives_a_restart(lake):
    """A ``running`` row from a dead process must not block the dataset forever."""
    path = lake / "_ingest" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "id": "abc123",
                    "dataset": "bars_daily",
                    "params": {},
                    "status": "running",
                    "message": "日线 2026-09-01",
                    "progress": 0.4,
                    "queued_at": "2026-10-31T20:00:00",
                    "started_at": "2026-10-31T20:00:01",
                }
            ]
        ),
        encoding="utf-8",
    )

    # Opening the ledger migrates the old JSON file; the row is history now.
    ingest._reset_for_tests()
    restored = ingest.job("abc123")

    assert restored is not None
    assert restored["status"] == "failed"
    assert "重启" in (restored["error"] or "")
    assert not ingest.active()
    # And it really is in sqlite now, not just in this process's memory.
    assert (lake / "_ingest" / "_jobs.sqlite").exists()


def test_progress_and_notes_stay_outside_a_job():
    """Importing an ingester must not require a running job."""
    ingest.progress(0.5, "ignored")
    ingest.note("ignored")


def test_report_serialises_dates():
    report = ingest.IngestReport(dataset="bars_daily", rows=3, start="2026-09-01", end="2026-09-02")
    payload = report.to_dict()
    assert payload["rows"] == 3
    assert payload["start"] == "2026-09-01"
    assert dt.date(2026, 9, 1).isoformat() == payload["start"]


# ---------------------------------------------------------------------------
# Full-market backfill
# ---------------------------------------------------------------------------


def test_universe_normalises_every_source_shape():
    """Sina's ``sh600519`` must not fall through to the US branch."""
    from src.data.ingest import universe

    assert universe.cn_symbol("sh600519") == "600519.SH"
    assert universe.cn_symbol("sz000001") == "000001.SZ"
    assert universe.cn_symbol("bj920000") == "920000.BJ"
    assert universe.cn_symbol("600519.SH") == "600519.SH"
    assert universe.cn_symbol("000001") == "000001.SZ"
    assert universe.cn_symbol("920000") == "920000.BJ"
    assert universe.cn_symbol("AAPL") == ""
    assert universe.cn_symbol("60051") == ""
    assert universe.market_of("600519.SH") == "SH"
    assert universe.market_of("920000") == "BJ"


def test_sink_batches_per_year_instead_of_per_symbol(monkeypatch):
    """One write per year partition, not one per frame."""
    from src.data.ingest import bars as ingest_bars_module

    calls: list[tuple[int, str]] = []

    def _fake_save(frame, interval, *, source=""):
        calls.append((len(frame), str(frame["date"].dt.year.unique().tolist())))
        return len(frame)

    monkeypatch.setattr("src.data.bars.save_long_bars", _fake_save)
    sink = ingest_bars_module._BarSink("1d", flush_rows=10_000)

    for day in ("2025-12-30", "2026-01-05", "2026-01-06"):
        sink.add(
            pd.DataFrame(
                {
                    "symbol": ["600519.SH"],
                    "date": [pd.Timestamp(day)],
                    "open": [1.0],
                    "high": [1.0],
                    "low": [1.0],
                    "close": [1.0],
                    "volume": [1.0],
                }
            ),
            "tencent",
        )

    assert calls == []  # below the threshold: nothing written yet
    sink.flush(force=True)
    assert len(calls) == 2  # one per year, not one per frame
    assert sink.written == 3


def test_full_market_backfill_covers_the_whole_universe(lake, sessions, monkeypatch):
    from src.data.ingest import universe as universe_module

    symbols = [f"{600000 + i}.SH" for i in range(40)]
    monkeypatch.setattr(
        universe_module, "list_symbols", lambda **kwargs: list(symbols)
    )
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())

    payload = ingest.run(
        "bars_daily",
        start="2026-09-01",
        end="2026-09-11",
        universe_mode="full",
        workers=4,
    )

    report = payload["report"]
    assert payload["status"] == "done", payload
    assert report["symbols"] == 40
    assert report["rows"] == 40 * len(sessions("2026-09-01", "2026-09-11"))
    stored = bar_store.load_bars("1d")
    assert stored["symbol"].nunique() == 40


def test_full_market_backfill_skips_covered_symbols(lake, sessions, monkeypatch):
    from src.data.ingest import universe as universe_module

    symbols = [f"{600000 + i}.SH" for i in range(6)]
    monkeypatch.setattr(universe_module, "list_symbols", lambda **kwargs: list(symbols))
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())

    first = ingest.run(
        "bars_daily", start="2026-09-01", end="2026-09-11", universe_mode="full", workers=4
    )
    second = ingest.run(
        "bars_daily", start="2026-09-01", end="2026-09-11", universe_mode="full", workers=4
    )

    assert first["report"]["symbols"] == 6
    assert second["report"]["rows"] == 0
    assert any("已覆盖" in note for note in second["report"]["notes"])

    # A wider window is not covered by a symbol whose stored span starts later,
    # even though it ends at the same session.
    third = ingest.run(
        "bars_daily", start="2026-08-03", end="2026-09-11", universe_mode="full", workers=4
    )
    assert third["report"]["rows"] > 0
    assert third["report"]["symbols"] == 6


def test_full_backfill_lifts_the_session_cap(lake, sessions, monkeypatch):
    """A five-year window must not be silently truncated to 250 sessions."""
    from src.data.ingest import universe as universe_module

    monkeypatch.setattr(
        universe_module, "list_symbols", lambda **kwargs: ["600519.SH"]
    )
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())

    payload = ingest.run(
        "bars_daily",
        start="2021-11-01",
        end="2026-10-30",
        universe_mode="full",
        workers=2,
    )

    assert payload["status"] == "done", payload
    assert payload["report"]["sessions"] > 1000
    assert not any("超过单次上限" in note for note in payload["report"]["notes"])


# ---------------------------------------------------------------------------
# adj_factor repair
# ---------------------------------------------------------------------------
#
# The five-year backfill ran on sources without a factor, so the column is null
# across millions of correct unadjusted rows.  These tests pin what makes the
# repair safe: it never touches a price, the factor is a step function keyed on
# the 除权日 (not a per-row average), a symbol only counts as done when every
# stored row has one, and a failed fetch leaves the previous value alone.


def _seed_daily(symbols=("600519.SH", "000001.SZ"), days=("2026-09-01", "2026-09-02")):
    """Store unadjusted bars with a null factor, as a Tencent backfill would."""
    rows = []
    for offset, symbol in enumerate(symbols):
        for day in days:
            price = 10.0 + offset
            rows.append(
                {
                    "symbol": symbol,
                    "date": pd.Timestamp(day),
                    "open": price,
                    "high": price + 0.5,
                    "low": price - 0.5,
                    "close": price + 0.2,
                    "volume": 1000.0,
                    "amount": float("nan"),
                    "adj_factor": float("nan"),
                }
            )
    return bar_store.save_long_bars(pd.DataFrame(rows), "1d", source="sina")


def _stub_baostock(monkeypatch, events_by_symbol, failing=()):
    """Answer baostock's per-symbol query from a literal event table.

    Patching ``_query`` (not ``events``) keeps the step expansion, the write-back
    and the reporting under test — only the socket is replaced.
    """
    import src.data.ingest.bars as bars_module

    def _login(self):
        self._connected = True

    def _close(self):
        self._connected = False

    def _query(self, code):
        market, _, digits = code.partition(".")
        symbol = f"{digits}.{market.upper()}"
        if symbol in failing:
            raise RuntimeError("socket dropped")
        rows = events_by_symbol.get(symbol, [])
        return pd.DataFrame(rows, columns=["date", "factor"]).astype(
            {"date": "datetime64[ns]", "factor": "float64"}
        )

    monkeypatch.setattr(bars_module._BaostockFactors, "_login", _login)
    monkeypatch.setattr(bars_module._BaostockFactors, "close", _close)
    monkeypatch.setattr(bars_module._BaostockFactors, "_query", _query)


def test_factor_steps_apply_from_the_ex_dividend_date():
    """The factor changes *on* 除权日, and is 1.0 before the first recorded one."""
    import src.data.ingest.bars as bars_module

    events = pd.DataFrame(
        {"date": pd.to_datetime(["2026-06-26", "2026-12-18"]), "factor": [7.669257, 7.80]}
    )
    dates = pd.Series(pd.to_datetime(["2026-06-25", "2026-06-26", "2026-12-18"]))

    assert bars_module._factor_steps(dates, events).tolist() == [1.0, 7.669257, 7.8]
    # No events at all is "never paid anything", which is a factor of exactly 1.
    empty = pd.DataFrame(columns=["date", "factor"])
    assert bars_module._factor_steps(dates, empty).tolist() == [1.0, 1.0, 1.0]


def test_adj_factor_repair_fills_the_column_without_touching_prices(lake, monkeypatch):
    from src.data import lake as lake_module

    _seed_daily()
    _stub_baostock(
        monkeypatch,
        {
            "000001.SZ": [("2026-09-01", 3.5)],
            "600519.SH": [("2026-09-02", 2.0)],
        },
    )

    payload = ingest.run("bars_daily", mode="adj_factor")

    assert payload["status"] == "done", payload
    report = payload["report"]
    assert report["rows"] == 4
    assert report["symbols"] == 2
    assert report["sessions"] == 2
    assert report["source"] == "baostock"

    stored = lake_module.read_dataset("bars_daily")
    factors = stored.groupby("symbol")["adj_factor"].apply(list).to_dict()
    # 600519's event lands on the second session, so the first one keeps 1.0 —
    # the step function is keyed on the 除权日, not smeared across the window.
    assert factors == {"000001.SZ": [3.5, 3.5], "600519.SH": [1.0, 2.0]}
    # The prices were already right: the repair must leave them untouched, and
    # leave amount null rather than invent it.
    closes = stored.groupby("symbol")["close"].apply(list).to_dict()
    assert closes == {"000001.SZ": [11.2, 11.2], "600519.SH": [10.2, 10.2]}
    assert stored["volume"].tolist() == [1000.0] * 4
    assert stored["amount"].isna().all()
    assert set(stored["source"].astype(str)) == {"sina"}


def test_adj_factor_repair_skips_symbols_already_covered(lake, monkeypatch):
    events = {"000001.SZ": [("2026-09-01", 3.5)], "600519.SH": [("2026-09-01", 2.0)]}
    _seed_daily()
    _stub_baostock(monkeypatch, events)
    assert ingest.run("bars_daily", mode="adj_factor")["status"] == "done"

    calls: list[str] = []
    import src.data.ingest.bars as bars_module

    monkeypatch.setattr(
        bars_module._BaostockFactors, "_query", lambda self, code: calls.append(code) or pd.DataFrame()
    )
    payload = ingest.run("bars_daily", mode="adj_factor")

    assert payload["status"] == "done", payload
    assert payload["report"]["rows"] == 0
    assert calls == []
    assert any("无需修复" in note for note in payload["report"]["notes"])


def test_adj_factor_repair_revisits_a_symbol_with_one_null_row(lake, monkeypatch):
    """One missing row is enough to redo the symbol — no percentage slack."""
    from src.data import lake as lake_module

    _seed_daily()
    # A symbol whose bar exists but whose factor never got written: seed half.
    stored = lake_module.read_dataset("bars_daily")
    stored.loc[stored["symbol"] == "600519.SH", "adj_factor"] = 2.0
    lake_module.write_dataset(stored.drop(columns=["year"], errors="ignore"), "bars_daily")

    queried: list[str] = []
    import src.data.ingest.bars as bars_module

    def _query(self, code):
        queried.append(code)
        return pd.DataFrame(
            [("2026-09-01", 2.0)], columns=["date", "factor"]
        ).astype({"date": "datetime64[ns]"})

    monkeypatch.setattr(bars_module._BaostockFactors, "_login", lambda self: setattr(self, "_connected", True))
    monkeypatch.setattr(bars_module._BaostockFactors, "close", lambda self: None)
    monkeypatch.setattr(bars_module._BaostockFactors, "_query", _query)

    payload = ingest.run("bars_daily", mode="adj_factor")

    # Only the half-covered symbol is asked for; the complete one is skipped.
    assert queried == ["sz.000001"]
    assert payload["report"]["symbols"] == 1
    filled = lake_module.read_dataset("bars_daily")
    assert filled["adj_factor"].notna().all()


def test_adj_factor_repair_keeps_a_known_factor_when_the_fetch_fails(lake, monkeypatch):
    """A dropped socket must not blank a factor that was already correct."""
    from src.data import lake as lake_module

    _seed_daily()
    _stub_baostock(
        monkeypatch,
        {"000001.SZ": [("2026-09-01", 3.5)], "600519.SH": [("2026-09-01", 2.0)]},
    )
    ingest.run("bars_daily", mode="adj_factor")

    _stub_baostock(
        monkeypatch,
        {"600519.SH": [("2026-09-01", 9.9)]},
        failing={"000001.SZ"},
    )
    payload = ingest.run("bars_daily", mode="adj_factor", refresh=True)

    assert payload["status"] == "done", payload
    assert any("取数失败" in note for note in payload["report"]["notes"])
    stored = lake_module.read_dataset("bars_daily")
    factors = stored.groupby("symbol")["adj_factor"].apply(list).to_dict()
    assert factors["600519.SH"] == [9.9, 9.9]
    assert factors["000001.SZ"] == [3.5, 3.5]


def test_adj_factor_repair_leaves_beijing_null_and_says_so(lake, monkeypatch):
    """baostock rejects ``.BJ`` codes; those rows stay null and the report admits it."""
    from src.data import lake as lake_module

    _seed_daily(symbols=("600519.SH", "920060.BJ"))
    _stub_baostock(monkeypatch, {"600519.SH": [("2026-09-01", 2.0)]})

    payload = ingest.run("bars_daily", mode="adj_factor")

    assert payload["status"] == "done", payload
    assert payload["report"]["symbols"] == 1
    assert any("北交所" in note for note in payload["report"]["notes"])
    stored = lake_module.read_dataset("bars_daily")
    assert stored.loc[stored["symbol"] == "920060.BJ", "adj_factor"].isna().all()
    assert stored.loc[stored["symbol"] == "600519.SH", "adj_factor"].notna().all()


def test_adj_factor_repair_notes_symbols_without_corporate_actions(lake, monkeypatch):
    from src.data import lake as lake_module

    _seed_daily()
    _stub_baostock(monkeypatch, {"600519.SH": [("2026-09-01", 2.0)]})

    payload = ingest.run("bars_daily", mode="adj_factor")

    assert any("无除权记录" in note for note in payload["report"]["notes"])
    stored = lake_module.read_dataset("bars_daily")
    assert stored.loc[stored["symbol"] == "000001.SZ", "adj_factor"].tolist() == [1.0, 1.0]


def test_adj_factor_mode_is_not_a_price_ingest(lake, sessions, monkeypatch):
    """Choosing the repair must never reach for a bar source."""

    def _explode(*args: object, **kwargs: object):
        raise AssertionError("adj_factor mode must not fetch prices")

    _seed_daily()
    _stub_baostock(monkeypatch, {})
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _explode)

    payload = ingest.run("bars_daily", mode="adj_factor")

    assert payload["status"] == "done", payload
    assert payload["report"]["rows"] == 4  # every row now carries 1.0


def test_unknown_bars_mode_is_rejected(lake):
    """A typo in the mode must fail the job loudly, not silently ingest prices."""
    payload = ingest.run("bars_daily", mode="nonsense")

    assert payload["status"] == "failed"
    assert "未知的 bars_daily mode" in (payload.get("error") or "")


def test_a_factor_that_steps_backwards_is_reported_not_corrected(lake, monkeypatch):
    """baostock has bare declines; the repair flags them and keeps the vendor value."""
    from src.data import lake as lake_module

    _seed_daily(days=("2026-09-01", "2026-09-02", "2026-09-03"))
    _stub_baostock(
        monkeypatch,
        {"600519.SH": [("2026-09-02", 2.0), ("2026-09-03", 1.9)]},
    )

    payload = ingest.run("bars_daily", mode="adj_factor")

    assert payload["status"] == "done", payload
    assert any("因子在某一天回退" in note for note in payload["report"]["notes"])
    stored = lake_module.read_dataset("bars_daily")
    factors = stored.loc[stored["symbol"] == "600519.SH"].sort_values("date")["adj_factor"].tolist()
    # Written as given: the repair records the oddity, it does not invent a fix.
    assert factors == [1.0, 2.0, 1.9]


# ---------------------------------------------------------------------------
# Ledger: cross-process claim, lease, cancel, cursor
# ---------------------------------------------------------------------------
#
# These pin the four properties that make a multi-minute backfill survivable:
# a second process cannot write the same dataset, a dead run does not block it
# forever, a cancel stops at a boundary instead of mid-write, and an unfinished
# run hands its cursor to the next one.


def _ledger_at(lake):
    from src.data.ingest.ledger import Ledger

    return Ledger(lake / "_ingest" / "_jobs.sqlite")


def _seed_run(lake, *, job_id="other-proc", dataset="bars_daily", status="running",
              heartbeat_ago=0.0, job_key="k", cursor=None, owner="999@other-host"):
    import datetime as dt

    stamp = (dt.datetime.now() - dt.timedelta(seconds=heartbeat_ago)).isoformat(
        timespec="seconds"
    )
    _ledger_at(lake).save(
        {
            "id": job_id,
            "dataset": dataset,
            "params": {},
            "job_key": job_key,
            "status": status,
            "message": "另一进程在跑",
            "progress": 0.3,
            "cursor": cursor,
            "queued_at": stamp,
            "started_at": stamp if status != "queued" else None,
            "heartbeat_at": stamp,
            "owner": owner,
        }
    )


def test_a_live_run_in_another_process_blocks_the_dataset(lake, sessions):
    """The old lock was a module-level dict; a CLI run could ignore it."""
    _seed_run(lake, heartbeat_ago=0)

    with pytest.raises(ingest.IngestBusy) as caught:
        ingest.run("bars_daily", start="2026-09-01", end="2026-09-02")

    assert "other-proc" in str(caught.value)


def test_a_dead_run_is_reclaimed_after_its_lease(lake, sessions, monkeypatch):
    """``kill -9`` leaves a running row; the heartbeat is what frees it."""
    _seed_run(lake, heartbeat_ago=600)
    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _stub_tencent())

    payload = ingest.run(
        "bars_daily", start="2026-09-01", end="2026-09-02", symbols=["600519.SH"]
    )

    assert payload["status"] == "done", payload
    stale = ingest.job("other-proc")
    assert stale["status"] == "failed"
    assert "心跳" in (stale["error"] or "")


def test_cancel_stops_a_run_at_a_boundary_and_says_where(lake, sessions, monkeypatch):
    """Cooperative: the run finishes what is in flight, then reports the cursor."""
    import time as _time

    def _slow(symbol, start, end, **kwargs):
        _time.sleep(0.3)
        return _stub_tencent()(symbol, start, end)

    monkeypatch.setattr("src.data.ingest.bars._tushare_available", lambda: False)
    monkeypatch.setattr("src.data.ingest.bars._fetch_tencent_raw_daily", _slow)
    universe = [f"6000{i:02d}.SH" for i in range(40)]

    queued = ingest.submit(
        "bars_daily",
        start="2026-09-01",
        end="2026-09-11",
        symbols=universe,
        workers=4,
    )
    deadline = _time.monotonic() + 10
    while _time.monotonic() < deadline and not ingest.active():
        _time.sleep(0.02)
    ingest.cancel(queued["id"])

    status = ""
    while _time.monotonic() < deadline:
        status = (ingest.job(queued["id"]) or {}).get("status", "")
        if status in {"done", "failed", "cancelled"}:
            break
        _time.sleep(0.05)

    payload = ingest.job(queued["id"])
    assert payload["status"] == "cancelled", payload
    assert any("已取消" in note for note in payload["report"]["notes"])
    # It stopped partway, and the cursor says exactly how far.
    assert payload["cursor"]["done"] < payload["cursor"]["total"]
    assert payload["report"]["rows"] > 0
    assert not ingest.active()


def test_only_an_unfinished_run_hands_a_cursor_forward(lake):
    """A completed nightly run must not make the next click skip the new session."""
    ledger = _ledger_at(lake)
    _seed_run(lake, job_id="done-run", status="done", job_key="abc",
              cursor={"done": 10, "total": 10})
    assert ledger.resume_cursor("abc") is None

    _seed_run(lake, job_id="failed-run", status="failed", job_key="abc",
              cursor={"done": 4, "total": 10})
    assert ledger.resume_cursor("abc") == ("failed-run", {"done": 4, "total": 10})


def test_the_runner_hands_the_cursor_to_an_ingester_that_asks_for_it(lake, monkeypatch):
    seen: dict[str, object] = {}

    def _fake(*, start=None, end=None, cursor=None, deadline_s=0.0):
        seen["cursor"] = cursor
        return ingest.IngestReport(dataset="bars_daily", start=start, end=end)

    monkeypatch.setattr(ingest, "_callable_for", lambda name: _fake)
    params = {"start": "2026-09-01", "end": "2026-09-11"}
    _seed_run(
        lake,
        job_id="failed-run",
        status="failed",
        job_key=ingest._job_key("bars_daily", params),
        cursor={"session": "2026-09-05", "done": 120, "total": 140},
    )

    payload = ingest.run("bars_daily", **params)

    assert payload["status"] == "done", payload
    assert seen["cursor"] == {"session": "2026-09-05", "done": 120, "total": 140}



# ---------------------------------------------------------------------------
# Intraday resume
# ---------------------------------------------------------------------------
#
# The intraday feed only serves the current session, so a full-market run is
# ~5,500 sequential fetches.  The old shape buffered everything and wrote once
# at the end: the deadline cut it at the same symbol every time and the tail was
# never reached.  These pin the fix — write in chunks, and let the lake (not the
# dataset watermark) decide what is left.


def _stub_intraday_loader(monkeypatch, session="2026-11-02"):
    """Replace the Tencent loader and record which symbols were asked for."""
    calls: list[str] = []

    class _Loader:
        def fetch(self, codes, start_date, end_date, *, interval="5m", fields=None):
            calls.extend(codes)
            index = pd.date_range(f"{session} 09:30", periods=4, freq="5min")
            frame = pd.DataFrame(
                {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100.0},
                index=index,
            )
            return {code: frame.copy() for code in codes}

    monkeypatch.setattr(
        "backtest.loaders.registry.get_loader_cls_with_fallback", lambda name: _Loader
    )
    monkeypatch.setattr(
        "src.data.hub.resolve_as_of",
        # ``AsOf.date`` is a string in the real object; a stub returning a
        # ``date`` hid a crash on the first live call.
        lambda: type("AsOf", (), {"date": session})(),
    )
    return calls


def _seed_intraday(symbol: str, session: str = "2026-11-02") -> None:
    index = pd.date_range(f"{session} 09:30", periods=4, freq="5min")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100.0},
        index=index,
    )
    bar_store.save_bars({symbol: frame}, "5m", source="tencent")


def test_intraday_writes_every_pending_symbol(lake, monkeypatch):
    calls = _stub_intraday_loader(monkeypatch)

    payload = ingest.run(
        "bars_intraday", interval="5m", symbols=["600519.SH", "000001.SZ"]
    )

    assert payload["status"] == "done", payload
    assert sorted(calls) == ["000001.SZ", "600519.SH"]
    assert payload["report"]["rows"] == 8
    assert payload["report"]["symbols"] == 2
    assert payload["cursor"] == {"session": "2026-11-02", "done": 2, "total": 2}


def test_intraday_skips_what_the_lake_already_holds(lake, monkeypatch):
    """One stored symbol must not make the whole market look "done"."""
    _seed_intraday("600519.SH")
    calls = _stub_intraday_loader(monkeypatch)

    payload = ingest.run(
        "bars_intraday", interval="5m", symbols=["600519.SH", "000001.SZ"]
    )

    assert calls == ["000001.SZ"]
    assert payload["report"]["skipped"] == 1
    assert payload["report"]["symbols"] == 1
    assert payload["report"]["rows"] == 4


def test_intraday_says_nothing_to_do_when_the_session_is_covered(lake, monkeypatch):
    _seed_intraday("600519.SH")
    calls = _stub_intraday_loader(monkeypatch)

    payload = ingest.run("bars_intraday", interval="5m", symbols=["600519.SH"])

    assert calls == []
    assert payload["report"]["rows"] == 0
    assert any("均已在湖内" in note for note in payload["report"]["notes"])


def test_intraday_refresh_refetches_a_covered_session(lake, monkeypatch):
    _seed_intraday("600519.SH")
    calls = _stub_intraday_loader(monkeypatch)

    payload = ingest.run(
        "bars_intraday", interval="5m", symbols=["600519.SH"], refresh=True
    )

    assert calls == ["600519.SH"]
    assert payload["report"]["symbols"] == 1


def test_a_price_refresh_cannot_blank_a_column_it_did_not_fetch(lake):
    """Tencent has no factor: re-pulling a window must not erase the repaired one.

    This is not hypothetical — a real ``--refresh`` over three symbols blanked
    41 days of ``adj_factor`` that the baostock repair had just filled, because
    the upsert replaces the whole row.
    """
    from src.data import lake as lake_module

    _seed_daily()
    filled = lake_module.read_dataset("bars_daily").drop(columns=["year"], errors="ignore")
    filled["adj_factor"] = 2.5
    lake_module.write_dataset(filled, "bars_daily")

    # A second pull from a source without a factor: same symbols, same dates,
    # no adj_factor.  Prices must update; the factor must survive.
    _seed_daily(symbols=("600519.SH",), days=("2026-09-01", "2026-09-02"))

    stored = lake_module.read_dataset("bars_daily").sort_values(["symbol", "date"])
    assert stored["adj_factor"].tolist() == [2.5] * 4
    assert stored["close"].tolist() == [11.2, 11.2, 10.2, 10.2]


# ---------------------------------------------------------------------------
# Historical minute backfill (baostock date range)
# ---------------------------------------------------------------------------


def _minute_frame(days, *, bars: int = 2, volume: float = 100.0) -> pd.DataFrame:
    """A two-bar-per-session minute frame, stamped 09:35 onward."""
    stamps = []
    for day in days:
        base = pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=35)
        stamps.extend(base + pd.Timedelta(minutes=5 * k) for k in range(bars))
    return pd.DataFrame(
        {
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": volume,
            "amount": 10_000.0,
        },
        index=pd.DatetimeIndex(stamps, name="date"),
    )


def _stub_baostock_minutes(monkeypatch, frames, *, failing=None):
    """Replace the socket but keep parsing, buffering and writes under test."""
    import src.data.ingest.bars as bars_module

    calls: list[tuple[str, str, str, str]] = []
    failures = failing if failing is not None else set()

    def _login(self):
        self._connected = True

    def _close(self):
        self._connected = False

    def _query(self, code, start, end, freq):
        calls.append((code, start, end, freq))
        market, _, digits = code.partition(".")
        symbol = f"{digits}.{market.upper()}"
        if symbol in failures:
            raise RuntimeError("socket dropped")
        return frames.get(symbol, pd.DataFrame()).copy()

    monkeypatch.setattr(bars_module._BaostockMinutes, "_login", _login)
    monkeypatch.setattr(bars_module._BaostockMinutes, "close", _close)
    monkeypatch.setattr(bars_module._BaostockMinutes, "_query", _query)
    return calls


def test_minute_range_backfill_writes_every_requested_session(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)  # fixes as_of at 2026-11-02
    calls = _stub_baostock_minutes(
        monkeypatch, {"600519.SH": _minute_frame(("2026-09-01", "2026-09-02"))}
    )

    payload = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    assert payload["status"] == "done", payload
    assert calls == [("sh.600519", "2026-09-01", "2026-09-02", "5")]
    assert payload["report"]["rows"] == 4
    from src.data import lake as lake_module

    stored = lake_module.read_dataset("bars_intraday").sort_values("date")
    assert stored["date"].dt.strftime("%Y-%m-%d").unique().tolist() == [
        "2026-09-01",
        "2026-09-02",
    ]
    # The stub replaces ``_query``, i.e. the already-parsed shape, so units are
    # asserted where they are actually converted: the parse test below.
    assert stored["volume"].tolist() == [100.0] * 4
    assert stored["amount"].tolist() == [10_000.0] * 4
    assert set(stored["source"]) == {"baostock"}


def test_minute_range_backfill_is_idempotent(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(
        monkeypatch, {"600519.SH": _minute_frame(("2026-09-01", "2026-09-02"))}
    )
    ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    second = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    assert len(calls) == 1
    assert second["report"]["skipped"] == 1
    assert second["report"]["rows"] == 0
    assert any("均已在湖内" in note for note in second["report"]["notes"])


def test_minute_range_backfill_remembers_years_without_bars(lake, monkeypatch):
    """A 2024 listing has no 2019 bars; "no rows" must not mean "ask again"."""
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    first = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )
    second = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    assert len(calls) == 1
    assert first["report"]["rows"] == 0
    assert any("没有分钟线" in note for note in first["report"]["notes"])
    assert second["report"]["skipped"] == 1
    assert second["report"]["rows"] == 0


def test_minute_range_backfill_clamps_to_the_archive_start(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    payload = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2018-06-01",
        end="2019-01-05",
    )

    assert calls and calls[0][1] == "2019-01-02"
    assert any("2019-01-02" in note for note in payload["report"]["notes"])


def test_a_range_wholly_before_the_archive_is_refused(lake, monkeypatch):
    """2015 minute bars do not exist; saying so beats an empty "done"."""
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    payload = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2015-01-01",
        end="2015-01-05",
    )

    assert calls == []
    assert any("范围之外" in note for note in payload["report"]["notes"])


def test_minute_range_backfill_refuses_a_bar_size_baostock_lacks(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    payload = ingest.run(
        "bars_intraday",
        interval="1m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    assert calls == []
    assert any("5/15/30/60" in note for note in payload["report"]["notes"])


def test_minute_range_backfill_reports_beijing_as_unsupported(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(
        monkeypatch, {"600519.SH": _minute_frame(("2026-09-01",))}
    )

    payload = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["920001.BJ", "600519.SH"],
        start="2026-09-01",
        end="2026-09-01",
    )

    assert [call[0] for call in calls] == ["sh.600519"]
    assert any("北交所" in note for note in payload["report"]["notes"])


def test_minute_range_backfill_retries_a_symbol_that_failed(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    failing = {"600519.SH"}
    calls = _stub_baostock_minutes(
        monkeypatch, {"600519.SH": _minute_frame(("2026-09-01",))}, failing=failing
    )

    first = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-01",
    )
    assert first["report"]["rows"] == 0
    assert any("取数失败" in note for note in first["report"]["notes"])

    failing.clear()  # the socket comes back
    second = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-01",
    )

    assert len(calls) == 2
    assert second["report"]["rows"] == 2


def test_minute_range_backfill_stops_at_the_deadline_and_resumes(lake, monkeypatch):
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(
        monkeypatch,
        {
            "600519.SH": _minute_frame(("2026-09-01",)),
            "000001.SZ": _minute_frame(("2026-09-01",)),
        },
    )

    first = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH", "000001.SZ"],
        start="2026-09-01",
        end="2026-09-01",
        deadline_s=1e-9,
    )
    assert first["report"]["rows"] == 0
    assert any("时限" in note for note in first["report"]["notes"])

    second = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH", "000001.SZ"],
        start="2026-09-01",
        end="2026-09-01",
    )

    assert second["status"] == "done", second
    assert second["report"]["rows"] == 4
    assert len(calls) == 2


def test_baostock_minute_query_stamps_the_bar_and_converts_shares(monkeypatch):
    """baostock returns 20260601093500000 and 226869 shares; the lake wants a
    timestamp and 手.  A silent mismatch here poisons every minute-level study."""
    import baostock as bs

    import src.data.ingest.bars as bars_module

    class _Answer:
        fields = ["date", "time", "open", "high", "low", "close", "volume", "amount"]
        error_code = "0"
        error_msg = ""

        def __init__(self, rows):
            self._rows = rows
            self._at = -1

        def next(self):
            self._at += 1
            return self._at < len(self._rows)

        def get_row_data(self):
            return self._rows[self._at]

    rows = [["2021-06-01", "20210601093500000", "2222.0", "2224.0", "2215.0", "2216.0", "226869", "503477408.0"]]
    monkeypatch.setattr(bs, "query_history_k_data_plus", lambda *a, **k: _Answer(rows))

    client = bars_module._BaostockMinutes()
    client._connected = True
    frame = client._query("sh.600519", "2021-06-01", "2021-06-01", "5")

    assert frame.index[0] == pd.Timestamp("2021-06-01 09:35:00")
    assert frame["close"].iloc[0] == 2216.0
    assert frame["volume"].iloc[0] == pytest.approx(2268.69)
    assert frame["amount"].iloc[0] == pytest.approx(503477408.0)


def test_minute_attempts_are_kept_per_interval(lake, monkeypatch):
    """5m coverage says nothing about hourly: the attempt key carries the size.

    Without the prefix, a 5m range run leaves ``SYMBOL|year`` behind and every
    later 1h backfill reads that as "already asked", returning an empty "done".
    """
    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )
    hourly = ingest.run(
        "bars_intraday",
        interval="1h",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )

    assert [call[3] for call in calls] == ["5", "60"]
    assert hourly["report"]["skipped"] == 0


def test_attempt_records_written_before_the_prefix_mean_5m(lake, monkeypatch):
    """Legacy keys predate hourly support, so they can only describe 5m."""
    path = lake / "_ingest" / "intraday_attempts.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"600519.SH|2026": "2026-01-01"}', encoding="utf-8")

    _stub_intraday_loader(monkeypatch)
    calls = _stub_baostock_minutes(monkeypatch, {})

    five = ingest.run(
        "bars_intraday",
        interval="5m",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )
    assert calls == []
    assert five["report"]["skipped"] == 1

    hourly = ingest.run(
        "bars_intraday",
        interval="1h",
        symbols=["600519.SH"],
        start="2026-09-01",
        end="2026-09-02",
    )
    assert [call[3] for call in calls] == ["60"]
    assert hourly["report"]["skipped"] == 0


# ---------------------------------------------------------------------------
# A dead socket must not outlive its budget
# ---------------------------------------------------------------------------


def test_guarded_call_raises_instead_of_hanging(monkeypatch):
    """A reader stuck on a dead socket surfaces as an error, not as a hang.

    baostock never checks for a closed pipe, so the only way out is to drop the
    socket from another thread.  The guard must turn that into an exception: the
    truncated page it leaves behind still reports ``error_code 0``, and writing
    half a year of bars as success is worse than failing the symbol.
    """
    import time

    import src.data.ingest.bars as bars_module

    dropped: list[object] = []
    monkeypatch.setattr(bars_module, "_default_socket", lambda: "socket-1")
    monkeypatch.setattr(bars_module, "_drop_socket", lambda sock: dropped.append(sock))

    client = bars_module._BaostockMinutes()
    client._connected = True

    with pytest.raises(bars_module.BaostockTimeout):
        client._guarded(lambda: time.sleep(3) or "half a year of bars", budget=0.05)

    assert dropped == ["socket-1"]
    assert client._connected is False  # the caller must reconnect, not reuse it


def test_guarded_call_passes_a_prompt_answer_through(monkeypatch):
    import src.data.ingest.bars as bars_module

    dropped: list[object] = []
    monkeypatch.setattr(bars_module, "_default_socket", lambda: "socket-1")
    monkeypatch.setattr(bars_module, "_drop_socket", lambda sock: dropped.append(sock))

    client = bars_module._BaostockMinutes()
    client._connected = True

    assert client._guarded(lambda: 42, budget=5.0) == 42
    assert dropped == []
    assert client._connected is True


def test_the_budget_is_larger_than_the_slowest_real_request():
    """Five years of 5-minute bars measures 70–90s; the guard must not fire there.

    A budget below that turns every legitimate long backfill into a retry loop,
    which looks like "baostock is flaky" and hides the real cause.
    """
    import src.data.ingest.bars as bars_module

    assert bars_module.QUERY_BUDGET_S >= 150


# ---------------------------------------------------------------------------
# Lease reconciliation on read
# ---------------------------------------------------------------------------


def test_a_killed_run_leaves_the_running_list_on_read(lake):
    """Reading the queue reconciles it; a dead process must not look alive.

    Reconciliation used to happen only inside ``claim``, so a job whose process
    was killed stayed ``running`` until somebody happened to submit the *same*
    dataset again.  The console polled a job that could never finish.
    """
    _seed_run(lake, job_id="killed", heartbeat_ago=600)

    assert ingest.active() == []
    stale = ingest.job("killed")
    assert stale["status"] == "failed"
    assert "心跳" in (stale["error"] or "")


def test_a_healthy_run_stays_visible(lake):
    """Reconciling on read must not evict the job that is actually working."""
    _seed_run(lake, job_id="alive", heartbeat_ago=0)

    assert [row["id"] for row in ingest.active()] == ["alive"]
    assert (ingest.job("alive") or {}).get("status") == "running"


# ---------------------------------------------------------------------------
# Lease reconciliation on read
# ---------------------------------------------------------------------------


def test_a_killed_run_leaves_the_running_list_on_read(lake):
    """Reading the queue reconciles it; a dead process must not look alive.

    Reconciliation used to happen only inside ``claim``, so a job whose process
    was killed stayed ``running`` until somebody happened to submit the *same*
    dataset again.  The console polled a job that could never finish.
    """
    _seed_run(lake, job_id="killed", heartbeat_ago=600)

    assert ingest.active() == []
    stale = ingest.job("killed")
    assert stale["status"] == "failed"
    assert "心跳" in (stale["error"] or "")


def test_a_healthy_run_stays_visible(lake):
    """Reconciling on read must not evict the job that is actually working."""
    _seed_run(lake, job_id="alive", heartbeat_ago=0)

    assert [row["id"] for row in ingest.active()] == ["alive"]
    assert (ingest.job("alive") or {}).get("status") == "running"
