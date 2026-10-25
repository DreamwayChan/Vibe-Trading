"""Market-pick ranking, catalog BK mapping, unique OHLCV, and snapshot parse."""

from __future__ import annotations

from datetime import date
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
