"""Market-pick ranking, catalog BK mapping, unique OHLCV, and snapshot parse."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import pytest
from src.market_pick.boards import BoardUniverse, unique_symbols
from src.market_pick.models import BoardScore, MarketPickResult
from src.market_pick.scan import _classify_plans, _score_board, run_market_pick_scan
from src.market_pick.score import (
    BULL_PENALTY,
    CatalogBookGate,
    assign_board_scores,
    build_reason,
    load_catalog_gates,
    map_leader,
)
from src.market_pick.store import (
    AlreadyScannedToday,
    list_snapshot_dates,
    list_snapshot_summaries,
    load_day_snapshot,
    require_fresh_scan,
    save_day_snapshot,
)
from src.monitor.trend_regime import CSI300
from src.sector_pick.catalog import catalog_key_for_board


@pytest.fixture(autouse=True)
def _isolate_clist_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-scan clist counters / cooldown are module state; never leak them."""
    import src.market_pick.boards as boards

    monkeypatch.setattr(boards, "_tripped_until", None)
    monkeypatch.setattr(boards, "_RETRY_BACKOFF_S", 0.0)
    # Adaptive gate: zero gap so tests never sleep, and pristine gate state.
    monkeypatch.setattr(boards, "_clist_gap_s", lambda: 0.0)
    monkeypatch.setattr(boards, "_gap", 0.0)
    monkeypatch.setattr(boards, "_next_fire", 0.0)
    # Probe / escalation / persistence state: pristine, no disk round-trip.
    monkeypatch.setattr(boards, "_probe_pending", False)
    monkeypatch.setattr(boards, "_trip_state_loaded", True)
    monkeypatch.setattr(boards, "_trip_count", 0)
    monkeypatch.setattr(boards, "_probe_fails", 0)
    monkeypatch.setattr(boards, "_trip_day", None)
    monkeypatch.setattr(boards, "_scan_started_at", None)
    monkeypatch.setattr(boards, "market_pick_dir", lambda: tmp_path / "mp")
    boards._reset_clist_state()


def _board(
    bk: str,
    name: str,
    *,
    classified: int = 20,
    rs_share: float = 0.4,
    knife_share: float = 0.2,
    board_type: str = "industry",
    timing_mult: float | None = None,
) -> BoardScore:
    return BoardScore(
        bk=bk,
        name=name,
        board_type=board_type,  # type: ignore[arg-type]
        classified=classified,
        universe_size=classified,
        rs_n=int(round(rs_share * classified)),
        knife_n=int(round(knife_share * classified)),
        rs_share=rs_share,
        knife_share=knife_share,
        timing_mult=timing_mult,
    )


def test_chop_ranks_high_rs_ahead_of_knife() -> None:
    strong = _board("BK1615", "金属铜", rs_share=0.55, knife_share=0.10)
    weak = _board("BK0001", "杂板", rs_share=0.10, knife_share=0.50)
    winner, stance = assign_board_scores([weak, strong], market_regime="chop")
    assert stance == "full"
    assert winner is not None
    assert winner.bk == "BK1615"
    assert strong.rank == 1
    assert weak.rank == 2


def test_bull_does_not_push_full_timing() -> None:
    strong = _board("BK1615", "金属铜", rs_share=0.80, knife_share=0.05)
    winner, stance = assign_board_scores([strong], market_regime="bull")
    assert stance == "half_watch"
    assert winner is not None
    assert winner.live_score == pytest.approx(0.80 - 0.05 - BULL_PENALTY)
    assert "不加仓" in build_reason(winner, stance, market_regime="bull")


def test_bear_recommends_nothing() -> None:
    strong = _board("BK1615", "金属铜", rs_share=0.90, knife_share=0.0)
    winner, stance = assign_board_scores([strong], market_regime="bear")
    assert winner is None
    assert stance == "none"
    assert strong.rank == 1


def test_bear_index_still_counts_stock_rs_and_knife() -> None:
    """CSI 300 bear gates the pick; RS/knife still follow each name's own stack."""
    idx = pd.RangeIndex(200)
    market = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    rs_close = pd.Series([10.0 + i * 0.05 for i in range(200)], index=idx)
    knife_close = pd.Series([30.0 - i * 0.05 for i in range(200)], index=idx)
    data_map = {
        CSI300: pd.DataFrame({"close": market}, index=idx),
        "RS.SZ": pd.DataFrame({"close": rs_close}, index=idx),
        "KN.SZ": pd.DataFrame({"close": knife_close}, index=idx),
    }
    plans = _classify_plans(data_map, market, skip={CSI300})
    assert plans["RS.SZ"].action in ("wait", "sell_rally")
    assert plans["KN.SZ"].action in ("wait", "sell_rally")
    strong = _score_board(
        BoardUniverse("BK1", "强势板", "industry", ["RS.SZ"]), data_map, plans
    )
    weak = _score_board(
        BoardUniverse("BK2", "刀口板", "industry", ["KN.SZ"]), data_map, plans
    )
    assert strong.rs_share == pytest.approx(1.0)
    assert strong.knife_share == pytest.approx(0.0)
    assert weak.rs_share == pytest.approx(0.0)
    assert weak.knife_share == pytest.approx(1.0)


def test_missing_regime_is_display_only() -> None:
    strong = _board("BK1615", "金属铜", rs_share=0.90, knife_share=0.0)
    winner, stance = assign_board_scores([strong], market_regime=None)
    assert winner is None
    assert stance == "none"


def test_undersized_boards_are_unranked() -> None:
    tiny = _board("BK1412", "铜加工", classified=1, rs_share=1.0, knife_share=0.0)
    cro = _board(
        "BK1600", "医疗研发外包", classified=29, rs_share=0.48, knife_share=0.0, timing_mult=1.0
    )
    winner, stance = assign_board_scores([tiny, cro], market_regime="chop")
    assert tiny.rank is None
    assert cro.rank == 1
    assert winner is not None
    assert winner.bk == "BK1600"
    assert stance == "full"


def test_below_ma20_rs_leader_is_not_recommended() -> None:
    cro = _board(
        "BK1600", "医疗研发外包", classified=29, rs_share=0.48, knife_share=0.0, timing_mult=0.5
    )
    coal = _board(
        "BK1494", "焦煤", classified=10, rs_share=0.40, knife_share=0.0, timing_mult=1.0
    )
    winner, stance = assign_board_scores([cro, coal], market_regime="chop")
    assert cro.rank == 1
    assert coal.rank == 2
    assert cro.skip_reason == "等权均价低于 MA20"
    assert winner is not None
    assert winner.bk == "BK1494"
    assert stance == "full"
    text = build_reason(winner, stance, market_regime="chop", map_row=cro)
    assert "焦煤" in text
    assert "医疗研发外包" in text
    assert "MA20" in text


def test_empty_catalog_book_is_not_recommended() -> None:
    cro = _board(
        "BK1600", "医疗研发外包", classified=29, rs_share=0.48, knife_share=0.0, timing_mult=1.0
    )
    coal = _board(
        "BK1494", "焦煤", classified=10, rs_share=0.40, knife_share=0.0, timing_mult=1.0
    )
    gates = {
        "cro": CatalogBookGate(
            book_healthy=False, holding_count=0, skip_reason="策略残仓：持仓 0 只"
        )
    }
    winner, stance = assign_board_scores(
        [cro, coal], market_regime="chop", catalog_gates=gates
    )
    assert winner is not None
    assert winner.bk == "BK1494"
    assert cro.skip_reason and "残仓" in cro.skip_reason
    assert map_leader([cro, coal]) is cro


def test_missing_timing_fails_open() -> None:
    strong = _board("BK1615", "金属铜", rs_share=0.55, knife_share=0.10)
    assert strong.timing_mult is None
    winner, stance = assign_board_scores([strong], market_regime="chop")
    assert stance == "full"
    assert winner is not None
    assert winner.bk == "BK1615"
    assert winner.skip_reason is None


def test_all_below_ma20_recommends_nothing() -> None:
    cro = _board(
        "BK1600", "医疗研发外包", classified=29, rs_share=0.48, knife_share=0.0, timing_mult=0.5
    )
    winner, stance = assign_board_scores([cro], market_regime="chop")
    assert winner is None
    assert stance == "half_watch"
    text = build_reason(winner, stance, market_regime="chop", map_row=cro)
    assert "没有可执行的前排板块" in text
    assert "医疗研发外包" in text


def test_catalog_bk_union_and_aliases() -> None:
    assert catalog_key_for_board("BK1615", "金属铜") == "copper"
    assert catalog_key_for_board("BK1600", "医疗研发外包") == "cro"
    assert catalog_key_for_board("BK0899", "CRO") == "cro0899"
    assert catalog_key_for_board("BK1450") == "ac"
    assert catalog_key_for_board("BK1400") == "ac"
    assert catalog_key_for_board("BK1326") == "semeq"
    assert catalog_key_for_board("BK1136") == "compute"
    assert catalog_key_for_board("BK1340") == "compute"
    assert catalog_key_for_board("BK1138") == "compute"
    assert catalog_key_for_board("BK9999", "AI应用") == "aiapp"
    assert catalog_key_for_board("BK0000", "没有这个板") is None


def test_unique_symbols_dedupes_across_boards() -> None:
    boards = [
        BoardUniverse("BK1", "A", "industry", ["000001.SZ", "600000.SH"]),
        BoardUniverse("BK2", "B", "concept", ["600000.SH", "000002.SZ"]),
    ]
    assert unique_symbols(boards) == ["000001.SZ", "600000.SH", "000002.SZ"]


def test_settled_end_never_asks_for_today() -> None:
    """The OHLCV end date must be settled, otherwise nothing is ever cached.

    ``loader_cache_put`` skips a range ending today, so ``end=today`` meant a
    full ~4600-symbol re-download on every cold start and every dev reload.
    """
    from src.market_pick.scan import _settled_end

    # Wednesday: fetch through Tuesday.
    assert _settled_end(date(2026, 10, 28), today=date(2026, 10, 28)) == date(2026, 10, 27)
    # Monday / Sunday / Saturday all resolve to the same Friday, so a weekend
    # does not re-download the universe three times.
    friday = date(2026, 10, 23)
    for day in (date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)):
        assert _settled_end(day, today=day) == friday
    # A historical scan is already settled and must not be shifted.
    assert _settled_end(date(2024, 1, 2), today=date(2026, 10, 28)) == date(2024, 1, 2)


def test_scan_requests_settled_end_date() -> None:
    seen: list[tuple[str, str]] = []

    def fake_fetch(symbols, start, end, source, interval):
        seen.append((start, end))
        empty = pd.DataFrame({"close": []})
        return {code: empty for code in symbols}, {}

    boards = [BoardUniverse("BK1", "A", "industry", ["000001.SZ"])]
    run_market_pick_scan(as_of=date.today(), boards=boards, fetch_ohlcv=fake_fetch)
    assert len(seen) == 1
    _, end = seen[0]
    from src.market_pick.scan import _settled_end

    assert end == _settled_end(date.today()).isoformat()
    assert end < date.today().isoformat()
    # The whole window hangs off the settled end, so the cache key does not
    # shift just because the calendar day advanced.
    start = date.fromisoformat(seen[0][0])
    assert (date.fromisoformat(end) - start).days == 400


def test_scan_fetches_unique_symbols_once() -> None:
    calls: list[list[str]] = []

    def fake_fetch(symbols, start, end, source, interval):
        calls.append(list(symbols))
        empty = pd.DataFrame({"close": []})
        return {code: empty for code in symbols}, {}

    boards = [
        BoardUniverse("BK1", "A", "industry", ["000001.SZ", "600000.SH"]),
        BoardUniverse("BK2", "B", "concept", ["600000.SH", "000002.SZ"]),
    ]
    result = run_market_pick_scan(
        as_of=date(2024, 1, 2),
        boards=boards,
        fetch_ohlcv=fake_fetch,
    )
    assert len(calls) == 1
    fetched = calls[0]
    assert fetched.count("000001.SZ") == 1
    assert fetched.count("600000.SH") == 1
    assert fetched.count("000002.SZ") == 1
    assert CSI300 in fetched
    assert result.unique_symbols == 3
    assert result.recommended_bk is None


def test_scan_flags_partial_member_coverage() -> None:
    """A board with no resolved members must surface as a degraded screen."""

    def fake_fetch(symbols, start, end, source, interval):
        return {}, {}

    boards = [
        BoardUniverse("BK1", "A", "industry", ["000001.SZ"]),
        BoardUniverse("BK2", "B", "concept", []),
    ]
    result = run_market_pick_scan(
        as_of=date(2024, 1, 2),
        boards=boards,
        fetch_ohlcv=fake_fetch,
    )
    assert result.member_coverage == 0.5
    assert result.degraded_reason is not None
    assert "50%" in result.degraded_reason


def test_scan_full_coverage_has_no_degraded_note() -> None:
    def fake_fetch(symbols, start, end, source, interval):
        return {}, {}

    boards = [BoardUniverse("BK1", "A", "industry", ["000001.SZ"])]
    result = run_market_pick_scan(
        as_of=date(2024, 1, 2),
        boards=boards,
        fetch_ohlcv=fake_fetch,
    )
    assert result.member_coverage == 1.0
    assert result.degraded_reason is None


def test_legacy_snapshot_without_new_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / ".vibe-trading" / "market_pick" / "history"
    path.mkdir(parents=True)
    (path / "2024-01-02.json").write_text(
        '{"as_of":"2024-01-02","scanned_at":"2024-01-02T00:00:00+00:00","boards":[{"bk":"BK1615","name":"金属铜"}]}',
        encoding="utf-8",
    )
    snap = load_day_snapshot("2024-01-02")
    assert snap is not None
    assert snap.as_of == "2024-01-02"
    assert snap.stance == "none"
    assert snap.unique_symbols == 0
    assert snap.boards[0].bk == "BK1615"
    assert snap.boards[0].rs_share is None


def test_same_day_scan_blocked_without_force(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = MarketPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        stance="full",
        reason="ok",
    )
    save_day_snapshot(result)
    try:
        require_fresh_scan("2024-01-02", force=False)
        raise AssertionError("expected AlreadyScannedToday")
    except AlreadyScannedToday as exc:
        assert exc.result.as_of == "2024-01-02"
    require_fresh_scan("2024-01-02", force=True)


def test_same_day_save_overwrites_history_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    first = MarketPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        stance="full",
        reason="morning",
        recommended_bk="BK1615",
        boards=[_board("BK1615", "金属铜")],
    )
    save_day_snapshot(first)
    second = MarketPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T08:00:00+00:00",
        stance="none",
        reason="afternoon",
        recommended_bk=None,
        boards=[_board("BK0001", "杂板")],
    )
    save_day_snapshot(second)
    assert list_snapshot_dates() == ["2024-01-02"]
    snap = load_day_snapshot("2024-01-02")
    assert snap is not None
    assert snap.reason == "afternoon"
    assert snap.recommended_bk is None
    rows = list_snapshot_summaries()
    assert len(rows) == 1
    assert rows[0]["as_of"] == "2024-01-02"
    assert rows[0]["recommended_name"] is None
    assert rows[0]["board_count"] == 1


def test_history_summaries_newest_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    save_day_snapshot(
        MarketPickResult(
            as_of="2024-01-01",
            scanned_at="2024-01-01T00:00:00+00:00",
            stance="none",
            reason="old",
        )
    )
    save_day_snapshot(
        MarketPickResult(
            as_of="2024-01-03",
            scanned_at="2024-01-03T00:00:00+00:00",
            stance="full",
            reason="new",
            recommended_bk="BK1615",
            boards=[_board("BK1615", "金属铜")],
        )
    )
    rows = list_snapshot_summaries(limit=30)
    assert [row["as_of"] for row in rows] == ["2024-01-03", "2024-01-01"]
    assert rows[0]["recommended_name"] == "金属铜"
    assert list_snapshot_summaries(limit=1)[0]["as_of"] == "2024-01-03"


def test_catalog_gates_read_empty_monitor_book(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.monitor.models import MonitorDaySnapshot
    from src.monitor.store import save_day_snapshot as save_monitor_day
    from src.sector_pick.catalog import recipe_by_key

    recipe = recipe_by_key("cro")
    assert recipe is not None
    save_monitor_day(
        MonitorDaySnapshot(
            as_of="2026-08-31",
            scanned_at="2026-08-31T00:00:00+00:00",
            run_id=recipe.run_id,
            holdings=[],
        )
    )
    gates = load_catalog_gates("2026-08-31")
    assert "cro" in gates
    assert gates["cro"].holding_count == 0
    assert gates["cro"].book_healthy is False


def test_catalog_gates_treat_wait_decision_as_untradable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.monitor.models import HoldingRow, MonitorDaySnapshot
    from src.monitor.store import save_day_snapshot as save_monitor_day
    from src.process.models import Decision
    from src.sector_pick.catalog import recipe_by_key

    recipe = recipe_by_key("cro")
    assert recipe is not None
    save_monitor_day(
        MonitorDaySnapshot(
            as_of="2026-08-31",
            scanned_at="2026-08-31T00:00:00+00:00",
            run_id=recipe.run_id,
            holdings=[
                HoldingRow(symbol="300404.SZ", weight=0.16, action="enter"),
                HoldingRow(symbol="600721.SH", weight=0.16, action="enter"),
                HoldingRow(symbol="688238.SH", weight=0.16, action="enter"),
            ],
            decision=Decision(
                action="wait",
                size_nav=0.0,
                reason="等待，不新开；建议规模 0.0% 净值。",
            ),
        )
    )
    gates = load_catalog_gates("2026-08-31")
    assert gates["cro"].book_healthy is False
    assert gates["cro"].holding_count == 0
    assert gates["cro"].skip_reason == "等待，建议规模 0%"


def test_clist_falls_back_when_push2_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    boards._working_clist_url = None
    monkeypatch.setattr(boards, "_RETRY_BACKOFF_S", 0.0)
    calls: list[str] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(url)
        if "push2delay" not in url:
            raise ConnectionError("Remote end closed connection without response")
        return {
            "data": {
                "diff": [{"f12": "BK1615", "f14": "金属铜"}],
                "total": 1,
            }
        }

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    rows = boards._fetch_board_list("industry")
    assert rows[0]["bk"] == "BK1615"
    assert any("push2delay.eastmoney.com" in url for url in calls)
    assert boards._working_clist_url is not None
    assert "push2delay" in boards._working_clist_url


def test_clist_skips_dead_push2_after_delay_works(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    boards._working_clist_url = "https://push2delay.eastmoney.com/api/qt/clist/get"
    calls: list[str] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(url)
        if "push2.eastmoney.com" in url and "push2delay" not in url:
            raise AssertionError("sticky host should not re-hit dead push2")
        return {"data": {"diff": [{"f12": "BK1137", "f14": "存储芯片"}], "total": 1}}

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    rows = boards._fetch_board_list("concept")
    assert rows[0]["bk"] == "BK1137"
    assert calls
    assert all("push2delay" in url for url in calls)


def test_to_ts_code_drops_b_shares() -> None:
    from src.market_pick.boards import _to_ts_code

    assert _to_ts_code({"f12": "900952", "f13": 1}) is None
    assert _to_ts_code({"f12": "200771", "f13": 0}) is None
    assert _to_ts_code({"f12": "000001", "f13": 0}) == "000001.SZ"


def _use_tmp_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    import src.market_pick.boards as boards

    cache = tmp_path / "market_pick"
    monkeypatch.setattr(boards, "market_pick_dir", lambda: cache)
    return cache


def test_clist_breaker_trips_and_stops_hitting_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_pick.boards as boards

    calls: list[str] = []

    def boom(url: str, *, params: dict) -> dict:
        calls.append(url)
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "get_json", boom)
    for _ in range(boards._TRIP_AFTER_FAILURES):
        with pytest.raises(Exception):
            boards._fetch_board_list("industry")
    assert boards._tripped_until is not None

    before = len(calls)
    with pytest.raises(boards.ClistUnavailable):
        boards._fetch_board_list("industry")
    # A tripped breaker must fail fast: no further socket attempts.
    assert len(calls) == before


def test_clist_request_budget_caps_one_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    monkeypatch.setenv("VIBE_TRADING_MARKET_PICK_MAX_CLIST", "2")
    calls: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(int(params["pn"]))
        return {"data": {"diff": [{"f12": "BK1", "f14": "板块"}], "total": 1}}

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    boards._clist_get({"pn": 1})
    boards._clist_get({"pn": 2})
    with pytest.raises(boards.ClistUnavailable):
        boards._clist_get({"pn": 3})
    assert calls == [1, 2]


def test_refresh_reuses_same_day_member_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    member_dir = cache / "members" / "2024-01-02"
    member_dir.mkdir(parents=True)
    (member_dir / "BK1615.json").write_text('["600362.SH"]', encoding="utf-8")
    fetched: list[str] = []

    def fake_fetch(bk: str) -> list[str]:
        fetched.append(bk)
        return ["000001.SZ"]

    monkeypatch.setattr(boards, "_fetch_constituents", fake_fetch)
    out = boards._load_constituents(date(2024, 1, 2), "BK1615", refresh=True)
    assert out == ["600362.SH"]
    # A same-day refresh must not re-pull members: that burst is what got the
    # IP banned in the first place.
    assert fetched == []


def test_board_list_pages_stop_at_reported_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_pick.boards as boards

    pages: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        pages.append(int(params["pn"]))
        # pz=1000 is requested; the server returns 100 per page here, which is
        # exactly the "server silently caps pz" case — pagination must follow
        # the actual row count instead of truncating the taxonomy.
        return {
            "data": {
                "diff": [{"f12": f"BK{params['pn']}{i:03d}", "f14": "板块"} for i in range(100)],
                "total": 101,
            }
        }

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    rows = boards._fetch_board_list("industry")
    assert pages == [1, 2]
    assert len(rows) == 200


def test_board_list_single_page_when_pz_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the server honors pz=1000 the whole taxonomy costs one call."""
    import src.market_pick.boards as boards

    pages: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        pages.append(int(params["pn"]))
        return {
            "data": {
                "diff": [{"f12": f"BK{params['pn']}{i:03d}", "f14": "板块"} for i in range(101)],
                "total": 101,
            }
        }

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    assert len(boards._fetch_board_list("industry")) == 101
    assert pages == [1]


def test_constituents_pages_stop_at_reported_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.market_pick.boards as boards

    pages: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        pages.append(int(params["pn"]))
        return {
            "data": {
                "diff": [{"f12": "600362", "f13": 1, "f14": "江西铜业"}],
                "total": 1,
            }
        }

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    assert boards._fetch_constituents("BK1615") == ["600362.SH"]
    assert pages == [1]


def test_board_list_falls_back_to_latest_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    stale_dir = cache / "boards"
    stale_dir.mkdir(parents=True)
    (stale_dir / "2024-01-01.json").write_text(
        '[{"bk":"BK1615","name":"金属铜","board_type":"industry"}]', encoding="utf-8"
    )

    def boom(board_type: str) -> list[dict[str, str]]:
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "_fetch_board_list", boom)
    rows = boards._load_board_list(date(2024, 1, 2), refresh=True)
    assert rows[0]["bk"] == "BK1615"
    assert boards._fallback_source_date == "2024-01-01"
    # Today's failed fetch must not overwrite the cache with partial data.
    assert not (stale_dir / "2024-01-02.json").exists()


def test_board_list_skips_stale_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    stale_dir = cache / "boards"
    stale_dir.mkdir(parents=True)
    (stale_dir / "2023-11-01.json").write_text(
        '[{"bk":"BK1615","name":"金属铜","board_type":"industry"}]', encoding="utf-8"
    )

    def boom(board_type: str) -> list[dict[str, str]]:
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "_fetch_board_list", boom)
    with pytest.raises(ConnectionError):
        boards._load_board_list(date(2024, 1, 2), refresh=True)


def test_member_cache_within_ttl_skips_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A member list inside the TTL is reused untouched — no clist call at all."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    member_dir = cache / "members" / "2024-01-01"
    member_dir.mkdir(parents=True)
    (member_dir / "BK1615.json").write_text(
        '["600362.SH", "000630.SZ"]', encoding="utf-8"
    )
    fetched: list[str] = []

    def fake_fetch(bk: str) -> list[str]:
        fetched.append(bk)
        return ["000001.SZ"]

    monkeypatch.setattr(boards, "_fetch_constituents", fake_fetch)
    symbols = boards._load_constituents(date(2024, 1, 2), "BK1615", refresh=True)
    assert symbols == ["600362.SH", "000630.SZ"]
    assert fetched == []


def test_stale_member_cache_is_served_without_inline_refetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Past the TTL the scan still uses the cache; refresh belongs to warm-up."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 30)
    stale_day = as_of - timedelta(days=20)  # inside the 30-day window, past TTL
    member_dir = cache / "members" / stale_day.isoformat()
    member_dir.mkdir(parents=True)
    (member_dir / "BK1615.json").write_text('["600362.SH"]', encoding="utf-8")
    attempts: list[str] = []

    def boom(bk: str) -> list[str]:
        attempts.append(bk)
        raise AssertionError("a scan must not re-pull members inline")

    monkeypatch.setattr(boards, "_fetch_constituents", boom)
    symbols = boards._load_constituents(as_of, "BK1615", refresh=False)
    assert symbols == ["600362.SH"]
    assert attempts == []
    # Warm-up is what queues the stale board for a refresh.
    missing, expired = boards._member_refresh_work(
        as_of, [{"bk": "BK1615", "name": "x", "board_type": "industry"}]
    )
    assert missing == []
    assert expired == ["BK1615"]


def test_constituents_return_empty_without_any_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    _use_tmp_cache(monkeypatch, tmp_path)

    def boom(bk: str) -> list[str]:
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "_fetch_constituents", boom)
    assert boards._load_constituents(date(2024, 1, 2), "BK1615", refresh=True) == []


def test_warm_work_pending_is_cache_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The warm-up gate must never touch the network on its own."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)

    def boom(url: str, *, params: dict) -> dict:
        raise AssertionError("pending check must not fetch")

    monkeypatch.setattr(boards, "get_json", boom)
    # No board-list cache yet → nothing to iterate.
    assert boards.warm_work_pending(as_of) is False

    board_dir = cache / "boards"
    board_dir.mkdir(parents=True)
    (board_dir / f"{as_of.isoformat()}.json").write_text(
        '[{"bk":"BK1","name":"A","board_type":"industry"}]', encoding="utf-8"
    )
    assert boards.warm_work_pending(as_of) is True

    member_dir = cache / "members" / as_of.isoformat()
    member_dir.mkdir(parents=True)
    (member_dir / "BK1.json").write_text('["600362.SH"]', encoding="utf-8")
    assert boards.warm_work_pending(as_of) is False


def test_warm_work_pending_falls_back_to_prior_board_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Today's taxonomy fetch being blocked must not disable the warm-up."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    board_dir = cache / "boards"
    board_dir.mkdir(parents=True)
    (board_dir / "2024-01-01.json").write_text(
        '[{"bk":"BK1","name":"A","board_type":"industry"}]', encoding="utf-8"
    )

    def boom(url: str, *, params: dict) -> dict:
        raise AssertionError("pending check must stay cache-only")

    monkeypatch.setattr(boards, "get_json", boom)
    assert boards.warm_work_pending(date(2024, 1, 2)) is True


def test_warm_member_cache_fills_missing_boards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    board_dir = cache / "boards"
    board_dir.mkdir(parents=True)
    (board_dir / f"{as_of.isoformat()}.json").write_text(
        '[{"bk":"BK1","name":"A","board_type":"industry"},'
        '{"bk":"BK2","name":"B","board_type":"concept"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(boards, "_fetch_constituents", lambda bk: ["600362.SH"])
    stats = boards.warm_member_cache(as_of, pace_s=0.0)
    assert stats["filled"] == 2
    assert (cache / "members" / as_of.isoformat() / "BK1.json").is_file()
    assert (cache / "members" / as_of.isoformat() / "BK2.json").is_file()
    # Second pass has nothing to do: warm-up is idempotent.
    again = boards.warm_member_cache(as_of, pace_s=0.0)
    assert again["filled"] == 0
    assert again["skipped"] == 2


def test_warm_member_cache_pauses_on_tripped_breaker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    board_dir = cache / "boards"
    board_dir.mkdir(parents=True)
    (board_dir / f"{as_of.isoformat()}.json").write_text(
        '[{"bk":"BK1","name":"A","board_type":"industry"}]', encoding="utf-8"
    )

    def cooled(bk: str) -> list[str]:
        raise boards.ClistUnavailable("banned")

    monkeypatch.setattr(boards, "_fetch_constituents", cooled)
    stats = boards.warm_member_cache(as_of, pace_s=0.0, max_waits=0)
    assert stats["filled"] == 0
    assert stats["deferred"] == 1


def test_scan_prefers_tushare_when_tokens_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_fetch(symbols, start, end, source, interval):
        seen["source"] = source
        return {}, {}

    monkeypatch.setattr(
        "backtest.loaders.tushare_pool.resolve_tushare_tokens",
        lambda: ["tok"],
    )
    boards = [
        BoardUniverse("BK1", "A", "industry", ["000001.SZ", "600000.SH"]),
    ]
    run_market_pick_scan(
        as_of=date(2024, 1, 2),
        boards=boards,
        fetch_ohlcv=fake_fetch,
        source="auto",
    )
    assert seen["source"] == "tushare"


def test_failing_clist_goes_single_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a failure is on record, the next call must not burn the retry grid."""
    import src.market_pick.boards as boards

    calls: list[str] = []

    def boom(url: str, *, params: dict) -> dict:
        calls.append(url)
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "get_json", boom)
    with pytest.raises(Exception):
        boards._fetch_board_list("industry")
    healthy_calls = len(calls)  # 2 hosts x 2 attempts while healthy
    with pytest.raises(Exception):
        boards._fetch_board_list("industry")
    assert len(calls) - healthy_calls == 1  # single shot, no retry grid


def test_failed_cooldown_probe_costs_one_request_and_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the cooldown, one probe fires; its failure lengthens the window."""
    import src.market_pick.boards as boards

    calls: list[str] = []

    def boom(url: str, *, params: dict) -> dict:
        calls.append(url)
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(boards, "get_json", boom)
    for _ in range(boards._TRIP_AFTER_FAILURES):
        with pytest.raises(Exception):
            boards._fetch_board_list("industry")
    assert boards._tripped_until is not None
    assert boards._trip_count == 1
    first_window = boards._tripped_until - time.monotonic()
    before_probe = len(calls)

    # Cooldown elapses: the next call is a one-shot probe.
    boards._tripped_until = time.monotonic() - 1
    boards._reset_clist_state()
    with pytest.raises(boards.ClistUnavailable):
        boards._fetch_board_list("industry")
    assert len(calls) - before_probe == 1  # one request, not a retry grid
    assert boards._tripped_until is not None

    # A failed probe is not a new trip: it must not reset the ladder to the
    # base cooldown, and the next quiet window must be longer than the last.
    assert boards._trip_count == 1
    assert boards._probe_fails == 1
    assert boards._tripped_until - time.monotonic() > first_window


def test_consecutive_probe_failures_keep_doubling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each failed probe widens the window instead of restarting the escalation."""
    import src.market_pick.boards as boards

    monkeypatch.setattr(boards, "random", type("R", (), {"uniform": staticmethod(lambda a, b: 1.0)}))
    boards._trip_count = 1
    boards._trip_day = date.today()

    windows = [boards._trip_cooldown(probe_failure=True) for _ in range(4)]
    assert windows == sorted(windows)
    assert windows[0] == boards._clist_cooldown_s() * 2  # 30min, not 15min again
    assert windows[1] == boards._clist_cooldown_s() * 4
    assert windows[-1] <= boards._TRIP_COOLDOWN_MAX_S
    assert boards._trip_count == 1  # probes never inflate the trip counter
    assert boards._probe_fails == 4


def test_probe_budget_exhausted_closes_until_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the day's probes are spent, no further request is even attempted."""
    import src.market_pick.boards as boards

    calls: list[str] = []
    monkeypatch.setattr(boards, "get_json", lambda url, *, params: calls.append(url) or {})
    boards._probe_fails = boards._max_probes_per_day()
    boards._trip_day = date.today()
    boards._tripped_until = time.monotonic() - 1

    assert boards._clist_ready() is False
    assert calls == []  # no probe was sent
    # Parked until midnight rather than retrying on the next scan.
    assert boards._tripped_until - time.monotonic() > 3600
    with pytest.raises(boards.ClistUnavailable):
        boards._clist_get({"pn": 1})
    assert calls == []


def test_probe_budget_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart must not hand out a fresh probe budget mid-ban."""
    import src.market_pick.boards as boards

    monkeypatch.setattr(boards, "market_pick_dir", lambda: tmp_path / "mp")
    boards._probe_fails = 3
    boards._trip_count = 1
    boards._trip_day = date.today()
    boards._tripped_until = time.monotonic() + 600
    boards._save_trip_state()

    boards._probe_fails = 0
    boards._trip_count = 0
    boards._tripped_until = None
    boards._trip_state_loaded = False
    boards._load_trip_state()
    assert boards._probe_fails == 3
    assert boards._trip_count == 1
    assert boards._tripped_until is not None


def test_probe_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent caller must not spend a second probe on a banned endpoint."""
    import src.market_pick.boards as boards

    calls: list[str] = []
    monkeypatch.setattr(boards, "get_json", lambda url, *, params: calls.append(url) or {})
    readiness = iter([True, False])  # ready at entry, closed by the time we lock
    monkeypatch.setattr(boards, "_clist_ready", lambda: next(readiness))
    boards._probe_pending = True

    with pytest.raises(boards.ClistUnavailable):
        boards._clist_get({"pn": 1})
    assert calls == []


def test_successful_traffic_resets_probe_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A working endpoint means the ban lifted; probes are available again."""
    import src.market_pick.boards as boards

    monkeypatch.setattr(
        boards,
        "get_json",
        lambda url, *, params: {"data": {"diff": [{"f12": "BK1615", "f14": "金属铜"}], "total": 1}},
    )
    boards._probe_fails = 2
    boards._clist_get({"pn": 1})
    assert boards._probe_fails == 0


def test_cooldown_probe_success_reopens_clist(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    def ok(url: str, *, params: dict) -> dict:
        return {"data": {"diff": [{"f12": "BK1615", "f14": "金属铜"}], "total": 1}}

    boards._probe_pending = True
    monkeypatch.setattr(boards, "get_json", ok)
    payload = boards._clist_get({"pn": 1})
    assert payload["data"]["diff"]
    assert boards._probe_pending is False
    assert boards._tripped_until is None


def test_trip_cooldown_escalates_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    lengths = [boards._trip_cooldown() for _ in range(6)]
    assert lengths[1] > lengths[0]  # doubling
    assert lengths[-1] <= boards._TRIP_COOLDOWN_MAX_S * 1.11  # capped + jitter


def test_trip_state_survives_process_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    monkeypatch.setattr(boards, "market_pick_dir", lambda: tmp_path / "mp")
    boards._trip_cooldown()
    assert boards._tripped_until is not None

    # Simulate a fresh process: wipe in-memory state, reload from disk.
    boards._tripped_until = None
    boards._trip_state_loaded = False
    boards._load_trip_state()
    assert boards._tripped_until is not None
    assert boards._trip_count == 1
    assert boards._trip_state_loaded is True


def test_inline_cold_fill_respects_time_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A scan must not block for minutes on cold members: budget stops it."""
    import src.market_pick.boards as boards

    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("VIBE_TRADING_MARKET_PICK_COLD_SECONDS", "0.01")
    boards._scan_started_at = time.monotonic() - 5

    def boom(bk: str) -> list[str]:
        raise AssertionError("inline cold fill must respect the time budget")

    monkeypatch.setattr(boards, "_fetch_constituents", boom)
    assert boards._load_constituents(date(2024, 1, 2), "BK1", refresh=True) == []


def test_clist_gate_doubles_gap_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    # Opt back into the real base gap (the autouse fixture zeroes it).
    monkeypatch.setattr(boards, "_clist_gap_s", lambda: boards._DEFAULT_CLIST_GAP_S)
    boards._gap = 0.0
    boards._gate_note(True)
    assert boards._gap == boards._DEFAULT_CLIST_GAP_S
    boards._gate_note(False)
    assert boards._gap == pytest.approx(boards._DEFAULT_CLIST_GAP_S * 2)
    for _ in range(10):
        boards._gate_note(False)
    assert boards._gap == boards._CLIST_GAP_MAX_S


# --- Bulk membership sweep ---------------------------------------------------
# The per-board path cannot cover ~1000 boards under the WAF's call budget, so
# one all-market sweep carries each stock's industry/concept tags instead.


def _bulk_rows(
    board_names: list[str],
    per_board: int,
    kind: str = "industry",
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, name in enumerate(board_names):
        for slot in range(per_board):
            code = f"{600000 + index * per_board + slot:06d}"
            rows.append(
                {
                    "f12": code,
                    "f13": 1,
                    "f14": "股票",
                    "f100": name if kind == "industry" else "其他行业",
                    "f103": name if kind == "concept" else "-",
                }
            )
    return rows


def _board_metas(count: int, kind: str = "industry") -> list[dict[str, object]]:
    return [
        {"bk": f"BK{index:03d}", "name": f"板块{index}", "board_type": kind}
        for index in range(count)
    ]


def _seed_board_list(cache: Path, as_of: date, metas: list[dict[str, object]]) -> None:
    board_dir = cache / "boards"
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / f"{as_of.isoformat()}.json").write_text(
        json.dumps(metas, ensure_ascii=False), encoding="utf-8"
    )


def test_bulk_sweep_fills_many_boards_in_one_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """30 cold boards, one all-market request — this is what scales."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})

    rows = _bulk_rows([str(meta["name"]) for meta in metas], 20)
    calls: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(int(params["pn"]))
        return {"data": {"diff": rows, "total": len(rows)}}

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    assert boards.bulk_fill_members(as_of, metas) == 30
    assert calls == [1]
    filled = json.loads(
        (cache / "members" / as_of.isoformat() / "BK007.json").read_text(encoding="utf-8")
    )
    assert len(filled) == 20
    assert filled[0] == "600140.SH"


def test_bulk_sweep_never_overwrites_an_existing_member_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})
    member_dir = cache / "members" / as_of.isoformat()
    member_dir.mkdir(parents=True)
    (member_dir / "BK000.json").write_text('["600362.SH"]', encoding="utf-8")

    rows = _bulk_rows([str(meta["name"]) for meta in metas], 20)
    monkeypatch.setattr(
        boards, "get_json", lambda url, *, params: {"data": {"diff": rows, "total": len(rows)}}
    )
    boards.bulk_fill_members(as_of, metas)
    # The per-board file stays authoritative even though the sweep disagrees.
    assert json.loads((member_dir / "BK000.json").read_text(encoding="utf-8")) == ["600362.SH"]


def test_bulk_sweep_is_rejected_when_it_disagrees_with_trusted_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name-based mapping that contradicts known members must be discarded."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})
    member_dir = cache / "members" / as_of.isoformat()
    member_dir.mkdir(parents=True)
    trusted = json.dumps([f"{index:06d}.SZ" for index in range(1, 21)])
    for bk in ("BK000", "BK001", "BK002"):
        (member_dir / f"{bk}.json").write_text(trusted, encoding="utf-8")

    rows = _bulk_rows([str(meta["name"]) for meta in metas], 20)
    monkeypatch.setattr(
        boards, "get_json", lambda url, *, params: {"data": {"diff": rows, "total": len(rows)}}
    )
    assert boards.bulk_fill_members(as_of, metas) == 0
    state = json.loads((cache / "members" / "_bulk_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "rejected"
    assert "overlap" in " ".join(state["verdicts"])
    assert not (member_dir / "BK010.json").is_file()


def test_bulk_sweep_discards_a_truncated_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half sweep would cache truncated members for 14 days — never write it."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})
    monkeypatch.setenv("VIBE_TRADING_MARKET_PICK_BULK_CALLS", "1")

    rows = _bulk_rows([str(meta["name"]) for meta in metas], 20)
    calls: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(int(params["pn"]))
        return {"data": {"diff": rows, "total": 5000}}

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    assert boards.bulk_fill_members(as_of, metas) == 0
    state = json.loads((cache / "members" / "_bulk_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "aborted"
    assert not (cache / "members" / as_of.isoformat()).exists()
    # An incomplete sweep would repeat identically; do not burn a budget on it.
    before = len(calls)
    assert boards.bulk_fill_members(as_of, metas) == 0
    assert len(calls) == before


def test_bulk_sweep_runs_at_most_once_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})

    rows = _bulk_rows([str(meta["name"]) for meta in metas], 20)
    calls: list[int] = []

    def fake_get_json(url: str, *, params: dict) -> dict:
        calls.append(int(params["pn"]))
        return {"data": {"diff": rows, "total": len(rows)}}

    monkeypatch.setattr(boards, "get_json", fake_get_json)
    boards.bulk_fill_members(as_of, metas)
    first = len(calls)
    # A later scan the same day must not re-spend the calls.
    boards.bulk_fill_members(as_of, metas)
    assert len(calls) == first


def test_bulk_sweep_needs_enough_cold_boards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.market_pick.boards as boards

    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})

    def boom(url: str, *, params: dict) -> dict:
        raise AssertionError("a nearly warm cache must not trigger a sweep")

    monkeypatch.setattr(boards, "get_json", boom)
    metas = _board_metas(5)
    assert boards.bulk_fill_members(date(2024, 1, 2), metas) == 0


def test_bulk_shape_check_rejects_placeholder_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``f100`` arriving as ``-`` must be rejected, not written as empty boards."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(30)
    _seed_board_list(cache, as_of, metas)
    rows = [
        {"f12": f"{600000 + index:06d}", "f13": 1, "f14": "股票", "f100": "-", "f103": "-"}
        for index in range(300)
    ]
    monkeypatch.setattr(
        boards, "get_json", lambda url, *, params: {"data": {"diff": rows, "total": len(rows)}}
    )
    assert boards.bulk_fill_members(as_of, metas) == 0
    assert not (cache / "members" / as_of.isoformat()).exists()


def test_bulk_sweep_splits_multi_tag_concepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``f103`` is a delimited tag list; each tag is its own concept board."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(20, kind="concept")
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})

    names = [str(meta["name"]) for meta in metas]
    monkeypatch.setenv("VIBE_TRADING_MARKET_PICK_BULK_MIN_MISSING", "5")
    rows = []
    for index in range(60):
        rows.append(
            {
                "f12": f"{600000 + index:06d}",
                "f13": 1,
                "f14": "股票",
                "f100": "某行业",
                "f103": f"{names[index % 10]}，{names[10 + index % 10]}",
            }
        )
    monkeypatch.setattr(
        boards, "get_json", lambda url, *, params: {"data": {"diff": rows, "total": len(rows)}}
    )
    assert boards.bulk_fill_members(as_of, metas) == 20


def test_bulk_sweep_rejects_a_coarse_only_tagging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A field that names only a few top-level industries must not be trusted."""
    import src.market_pick.boards as boards

    cache = _use_tmp_cache(monkeypatch, tmp_path)
    as_of = date(2024, 1, 2)
    metas = _board_metas(40)
    _seed_board_list(cache, as_of, metas)
    monkeypatch.setattr(boards, "_BULK_MIN_CODES", {"industry": 10, "concept": 10})

    tagged = [str(meta["name"]) for meta in metas[:12]]
    rows = _bulk_rows(tagged, 20)
    monkeypatch.setattr(
        boards, "get_json", lambda url, *, params: {"data": {"diff": rows, "total": len(rows)}}
    )
    assert boards.bulk_fill_members(as_of, metas) == 0
    state = json.loads((cache / "members" / "_bulk_state.json").read_text(encoding="utf-8"))
    assert "12/40 boards tagged" in state["verdicts"][0]
    assert not (cache / "members" / as_of.isoformat()).exists()


def test_warm_up_orders_cold_boards_by_turnover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Quota is small, so liquid boards must be filled before dead ones."""
    import src.market_pick.boards as boards

    _use_tmp_cache(monkeypatch, tmp_path)
    boards_meta = [
        {"bk": "BK1", "name": "冷门", "board_type": "industry", "turnover": 1.0},
        {"bk": "BK2", "name": "最热", "board_type": "industry", "turnover": 9.0e8},
        {"bk": "BK3", "name": "温", "board_type": "concept", "turnover": 5.0e7},
    ]
    missing, expired = boards._member_refresh_work(date(2024, 1, 2), boards_meta)
    assert missing == ["BK2", "BK3", "BK1"]
    assert expired == []


def test_board_list_records_turnover(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.market_pick.boards as boards

    monkeypatch.setattr(
        boards,
        "get_json",
        lambda url, *, params: {
            "data": {"diff": [{"f12": "BK1615", "f14": "稀土", "f6": 1.25e9, "f3": 2.5}], "total": 1}
        },
    )
    rows = boards._fetch_board_list("industry")
    assert rows == [
        {"bk": "BK1615", "name": "稀土", "board_type": "industry", "turnover": 1.25e9}
    ]


# ---------------------------------------------------------------------------
# Bars: the lake is the source of record, the network only closes gaps
# ---------------------------------------------------------------------------


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


def _stored(frames, sources=None):
    from src.data.hub import StoredBars

    return StoredBars(
        interval="1D", frames=frames, sources=sources or {}, derived_from=None
    )


@pytest.fixture
def lake_bars(monkeypatch: pytest.MonkeyPatch):
    """Serve ``hub.bars_frame`` from memory; no parquet, no lake root, no I/O."""

    def install(frames, sources=None):
        import src.data.hub as hub

        monkeypatch.setattr(hub, "bars_frame", lambda *a, **k: _stored(frames, sources))

    return install


@pytest.fixture
def net_calls(monkeypatch: pytest.MonkeyPatch):
    """Record every loader call and answer with the supplied frames."""
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def fake(symbols, *, start, end, source, interval):
        calls.append((start, end, tuple(symbols)))
        return {}, {s: "no OHLCV bars returned" for s in symbols}

    import src.market_pick.scan as scan

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", fake)
    return calls


def test_lake_window_is_used_without_touching_the_network(lake_bars, net_calls):
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tushare"}
    )

    import src.market_pick.scan as scan

    data, errors = scan._default_fetch(
        ["600519.SH"], "2024-01-01", "2024-01-03", "tushare", "1D"
    )

    assert net_calls == []
    assert list(data) == ["600519.SH"]
    assert errors == {}


def test_lake_tail_gap_fetches_only_the_missing_session(
    lake_bars, monkeypatch: pytest.MonkeyPatch
):
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tushare"}
    )
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def fake(symbols, *, start, end, source, interval):
        calls.append((start, end, tuple(symbols)))
        return {"600519.SH": _daily("2024-01-04")}, {}

    import src.market_pick.scan as scan

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", fake)

    data, errors = scan._default_fetch(
        ["600519.SH"], "2024-01-01", "2024-01-04", "tushare", "1D"
    )

    # The 400-day history stays local; only the day ingestion had not written yet
    # is requested.
    assert calls == [("2024-01-04", "2024-01-04", ("600519.SH",))]
    assert list(data["600519.SH"].index) == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
        pd.Timestamp("2024-01-04"),
    ]
    assert errors == {}


def test_holiday_gap_is_not_fanned_out_to_the_whole_market(
    lake_bars, net_calls
):
    symbols = [f"60{index:04d}.SH" for index in range(60)]
    lake_bars(
        {symbol: _daily("2024-01-02", "2024-01-03") for symbol in symbols},
        {symbol: "tushare" for symbol in symbols},
    )

    import src.market_pick.scan as scan

    data, errors = scan._default_fetch(
        symbols, "2024-01-01", "2024-01-08", "tushare", "1D"
    )

    # One CSI 300 probe answers "did the market trade"; the empty answer stops the
    # fan-out instead of asking 60 names for a bar that cannot exist.
    assert net_calls == [("2024-01-04", "2024-01-08", (CSI300,))]
    assert len(data) == 60
    assert errors == {}


def test_short_lake_history_is_pulled_whole(lake_bars, net_calls, monkeypatch):
    lake_bars(
        {"600519.SH": _daily("2024-03-01", "2024-03-04")}, {"600519.SH": "tushare"}
    )

    def fake(symbols, *, start, end, source, interval):
        net_calls.append((start, end, tuple(symbols)))
        return {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {}

    import src.market_pick.scan as scan

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", fake)

    data, _ = scan._default_fetch(
        ["600519.SH"], "2024-01-01", "2024-01-03", "tushare", "1D"
    )

    # A lake that starts three months into the lookback cannot classify the name.
    assert net_calls == [("2024-01-01", "2024-01-03", ("600519.SH",))]
    assert data["600519.SH"].index.min() == pd.Timestamp("2024-01-02")


def test_source_mismatch_refetches_instead_of_splicing(lake_bars, net_calls, monkeypatch):
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tencent"}
    )

    def fake(symbols, *, start, end, source, interval):
        net_calls.append((start, end, tuple(symbols)))
        return {"600519.SH": _daily("2024-01-02", "2024-01-03", "2024-01-04")}, {}

    import src.market_pick.scan as scan

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", fake)

    scan._default_fetch(["600519.SH"], "2024-01-01", "2024-01-04", "tushare", "1D")

    # Tencent-sourced history with a Tushare tail risks one bogus return at the
    # seam, so the name is pulled whole instead.
    assert net_calls == [("2024-01-01", "2024-01-04", ("600519.SH",))]


def test_suspended_name_keeps_its_lake_frame_without_an_error(lake_bars, net_calls):
    lake_bars(
        {"600519.SH": _daily("2024-01-02", "2024-01-03")}, {"600519.SH": "tushare"}
    )

    import src.market_pick.scan as scan

    data, errors = scan._default_fetch(
        ["600519.SH"], "2024-01-01", "2024-01-04", "tushare", "1D"
    )

    assert len(data["600519.SH"]) == 2
    # "no bar for this name" is a suspension, not a data failure.
    assert errors == {}


def test_empty_lake_falls_back_to_a_full_pull(lake_bars, net_calls):
    lake_bars({})

    import src.market_pick.scan as scan

    scan._default_fetch(["600519.SH"], "2024-01-01", "2024-01-04", "tushare", "1D")

    assert net_calls == [("2024-01-01", "2024-01-04", ("600519.SH",))]


def test_scan_stamps_the_data_centre_as_the_bars_source(lake_bars, monkeypatch):
    """A screen served entirely by the lake must say so on its result."""
    days = pd.bdate_range("2023-12-04", "2024-01-03")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": [10.0 + 0.01 * i for i in range(len(days))],
            "volume": 1000.0,
        },
        index=days,
    )
    symbols = ["000001.SZ", "600000.SH", CSI300]
    lake_bars(
        {symbol: frame.copy() for symbol in symbols},
        {symbol: "tushare" for symbol in symbols},
    )

    import src.market_pick.scan as scan

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(
        scan,
        "_apply_same_day_overlay",
        lambda data_map, *, as_of_date: (data_map, {}, {}),
    )

    result = run_market_pick_scan(
        as_of=date(2024, 1, 3),
        lookback_days=30,
        boards=[BoardUniverse("BK1", "A", "industry", ["000001.SZ", "600000.SH"])],
        source="tushare",
    )

    assert result.bars_source == "lake"
    assert result.to_dict()["bars_source"] == "lake"


def test_scan_reports_a_network_pull_when_the_lake_is_empty(lake_bars, monkeypatch):
    lake_bars({})

    import src.market_pick.scan as scan

    calls: list[tuple[str, str, str]] = []

    def fake(symbols, *, start, end, source, interval):
        calls.append((start, end, source))
        return {symbol: pd.DataFrame({"close": []}) for symbol in symbols}, {}

    monkeypatch.setattr(scan, "_fetch_universe_ohlcv", fake)
    monkeypatch.setattr(
        scan,
        "_apply_same_day_overlay",
        lambda data_map, *, as_of_date: (data_map, {}, {}),
    )

    result = run_market_pick_scan(
        as_of=date(2024, 1, 3),
        lookback_days=30,
        boards=[BoardUniverse("BK1", "A", "industry", ["000001.SZ"])],
        source="tushare",
    )

    assert result.bars_source == "network"
    assert calls == [("2023-12-04", "2024-01-03", "tushare")]


def test_injected_fetcher_leaves_the_bars_source_unobserved() -> None:
    """A test/replay fetcher never touches the lake, so the label must stay empty."""

    def fake_fetch(symbols, start, end, source, interval):
        return {}, {}

    result = run_market_pick_scan(
        as_of=date(2024, 1, 2),
        boards=[BoardUniverse("BK1", "A", "industry", ["000001.SZ"])],
        fetch_ohlcv=fake_fetch,
    )

    assert result.bars_source is None

