"""Unit tests for sector-pick scoring, catalog, and persistence."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import json

import pandas as pd
import pytest

from src.monitor.models import DayOps, HoldingRow
from src.sector_pick.catalog import (
    CATALOG,
    SectorRecipe,
    default_enabled_keys,
    load_run_display_title,
    recipe_by_key,
    validate_enabled_keys,
)
from src.sector_pick.models import SectorPickResult, SectorScore
from src.sector_pick.score import (
    assign_live_scores,
    build_reason,
    build_untradable_reason,
    close_panel,
    evaluate_book,
    first_tradable,
    stamp_book,
    timing_and_momentum,
)
from src.sector_pick.scan import (
    _pick_tradable_book,
    _select_locked_book,
    run_sector_pick_scan,
    score_recipe,
)
from src.sector_pick.theme_lock import ThemeLock
from src.sector_pick.store import (
    commit_scan_result,
    list_history_series,
    list_snapshot_summaries,
    load_day_snapshot,
    load_state,
    save_state,
)


def test_catalog_has_unique_keys_and_recipes() -> None:
    keys = [row.key for row in CATALOG]
    assert len(keys) == 21
    assert len(set(keys)) == 21
    assert recipe_by_key("robot") is not None
    assert recipe_by_key("robot").variant == "f0_prime"
    assert recipe_by_key("broker").use_max_veto is True
    assert recipe_by_key("cro") is not None
    assert recipe_by_key("cro").confirm_ids == ("gtja191_171", "gtja191_075")
    assert recipe_by_key("cro").use_max_veto is False
    assert recipe_by_key("cro0899") is not None
    assert recipe_by_key("cro0899").confirm_ids == ("gtja191_171", "gtja191_054")
    assert recipe_by_key("missing") is None


def test_load_run_display_title_matches_monitor_rename(tmp_path: Path) -> None:
    run_dir = tmp_path / "20260725_184000_05_F0_robot_124_178"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text("# engine\n", encoding="utf-8")
    label = "F0'最优确认-人形机器人-124+178"
    (run_dir / "req.json").write_text(
        json.dumps({"title": label, "prompt": "ignored"}, ensure_ascii=False),
        encoding="utf-8",
    )
    title, present = load_run_display_title(run_dir.name, runs_root=tmp_path)
    assert present is True
    assert title == label
    missing_title, missing_present = load_run_display_title("nope", runs_root=tmp_path)
    assert missing_present is False
    assert missing_title is None
    annotated = recipe_by_key("robot").annotated_dict(runs_root=tmp_path)
    assert annotated["run_title"] == label
    assert annotated["run_missing"] is False
    assert annotated["name"] == "人形机器人"


def test_validate_enabled_keys_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown sector key"):
        validate_enabled_keys(["robot", "not-a-board"])
    assert validate_enabled_keys([]) == []
    assert validate_enabled_keys(["broker", "robot"]) == ["robot", "broker"]


def _close_trend(n: int = 40, start: float = 10.0, step: float = 0.2) -> pd.DataFrame:
    """Synthetic rising (or falling) equal-weight panel."""
    idx = pd.bdate_range("2024-01-02", periods=n)
    a = [start + i * step for i in range(n)]
    b = [start + 0.5 + i * step for i in range(n)]
    return pd.DataFrame({"AAA.SZ": a, "BBB.SZ": b}, index=idx)


def test_timing_above_ma20() -> None:
    close = _close_trend(step=0.3)
    metrics = timing_and_momentum(close)
    assert metrics["timing_mult"] == 1.0
    assert metrics["ew_ma20_ratio"] is not None and metrics["ew_ma20_ratio"] >= 1.0
    assert metrics["ret_20d"] is not None and metrics["ret_20d"] > 0


def test_timing_below_ma20() -> None:
    close = _close_trend(step=-0.3)
    metrics = timing_and_momentum(close)
    assert metrics["timing_mult"] == 0.5
    assert metrics["ew_ma20_ratio"] is not None and metrics["ew_ma20_ratio"] < 1.0


def test_pick_winner_prefers_boards_above_ma20() -> None:
    above_a = SectorScore(
        key="a", name="A", panel="A", run_id="r_a", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
    )
    above_b = SectorScore(
        key="b", name="B", panel="B", run_id="r_b", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.02, confirm_pass_rate=0.80,
    )
    below = SectorScore(
        key="c", name="C", panel="C", run_id="r_c", variant="f1_max",
        timing_mult=0.5, ret_20d=0.40, confirm_pass_rate=0.99,
    )
    winner, stance = assign_live_scores([above_a, above_b, below])
    assert stance == "full"
    assert winner is not None
    assert winner.key in {"a", "b"}
    assert below.rank is not None and below.rank > (above_a.rank or 0)
    assert below.rank > (above_b.rank or 0)


def test_pick_winner_half_watch_when_all_below_ma20() -> None:
    weak = SectorScore(
        key="x", name="X", panel="X", run_id="r_x", variant="f0_prime",
        timing_mult=0.5, ret_20d=0.01, confirm_pass_rate=0.2,
    )
    strong = SectorScore(
        key="y", name="Y", panel="Y", run_id="r_y", variant="f0_prime",
        timing_mult=0.5, ret_20d=0.08, confirm_pass_rate=0.6,
    )
    winner, stance = assign_live_scores([weak, strong])
    assert stance == "half_watch"
    assert winner is not None
    assert winner.key == "y"


def test_pick_winner_none_when_all_errors() -> None:
    broken = SectorScore(
        key="z", name="Z", panel="Z", run_id="r_z", variant="f0_prime",
        enabled=True, error="run not found",
    )
    winner, stance = assign_live_scores([broken])
    assert winner is None
    assert stance == "none"


def test_chop_overlay_adds_rs_and_penalizes_knife() -> None:
    strong = SectorScore(
        key="rs", name="RS", panel="R", run_id="r_rs", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
        rs_share=1.0, knife_share=0.0,
    )
    knife = SectorScore(
        key="kn", name="KN", panel="K", run_id="r_kn", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
        rs_share=0.0, knife_share=1.0,
    )
    winner, stance = assign_live_scores([strong, knife], market_regime="chop")
    assert stance == "full"
    assert winner is not None and winner.key == "rs"
    assert (strong.live_score or 0) > (knife.live_score or 0)


def test_chop_overlay_does_not_override_timing_gap() -> None:
    above = SectorScore(
        key="a", name="A", panel="A", run_id="r_a", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
        knife_share=1.0, rs_share=0.0,
    )
    below = SectorScore(
        key="b", name="B", panel="B", run_id="r_b", variant="f0_prime",
        timing_mult=0.5, ret_20d=0.10, confirm_pass_rate=0.50,
        rs_share=1.0, knife_share=0.0,
    )
    winner, stance = assign_live_scores([above, below], market_regime="chop")
    assert stance == "full"
    assert winner is not None and winner.key == "a"


def test_bull_strips_timing_bonus_and_caps_stance() -> None:
    above = SectorScore(
        key="a", name="A", panel="A", run_id="r_a", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
    )
    below = SectorScore(
        key="c", name="C", panel="C", run_id="r_c", variant="f1_max",
        timing_mult=0.5, ret_20d=0.40, confirm_pass_rate=0.99,
    )
    winner, stance = assign_live_scores([above, below], market_regime="bull")
    assert stance == "half_watch"
    assert winner is not None and winner.key == "c"


def test_bear_stance_is_none_even_with_a_winner() -> None:
    row = SectorScore(
        key="a", name="A", panel="A", run_id="r_a", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, confirm_pass_rate=0.50,
    )
    winner, stance = assign_live_scores([row], market_regime="bear")
    assert winner is not None and winner.key == "a"
    assert stance == "none"


def test_holding_row_parses_without_trend() -> None:
    row = HoldingRow.from_dict({"symbol": "000001.SZ", "weight": 0.16, "action": "enter"})
    assert row.trend is None
    assert row.action == "enter"


def test_sector_pick_result_roundtrips_decision() -> None:
    from src.process.compose import compose

    holdings = [HoldingRow(symbol="000001.SZ", weight=0.1, action="hold")]
    decision = compose(market_regime="chop", f0_stance="full", holdings=holdings)
    result = SectorPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        stance="full",
        reason="overlay",
        holdings=holdings,
        decision=decision,
    )
    restored = SectorPickResult.from_dict(result.to_dict())
    assert restored is not None
    assert restored.decision is not None
    assert restored.decision.action == decision.action
    assert restored.decision.mandate_hash == decision.mandate_hash


def test_sector_pick_result_roundtrips_lock_fields() -> None:
    result = SectorPickResult(
        as_of="2024-01-02",
        scanned_at="2024-01-02T00:00:00+00:00",
        recommended_key="copper",
        locked_key="copper",
        map_key="robot",
        challenger_key="robot",
        challenger_days=1,
        challenger_as_of="2024-01-02",
        lock_event="held",
        lock_reason="锁定续持 金属铜。",
    )
    loaded = SectorPickResult.from_dict(result.to_dict())
    assert loaded is not None
    assert loaded.locked_key == "copper"
    assert loaded.map_key == "robot"
    assert loaded.challenger_days == 1
    assert loaded.lock_event == "held"


def test_sector_pick_result_parses_legacy_snapshot() -> None:
    result = SectorPickResult.from_dict(
        {
            "as_of": "2024-01-02",
            "scanned_at": "2024-01-02T00:00:00+00:00",
            "recommended_key": "robot",
            "stance": "full",
            "reason": "ok",
            "sectors": [],
            "holdings": [{"symbol": "000001.SZ", "weight": 0.1, "action": "hold"}],
        }
    )
    assert result is not None
    assert result.market_regime is None
    assert result.holdings[0].trend is None
    assert result.decision is None


def _eq_holdings(symbols: list[str]) -> list[HoldingRow]:
    weight = 1.0 / len(symbols)
    return [HoldingRow(symbol=code, weight=weight, action="hold") for code in symbols]


def test_evaluate_book_rejects_single_name_at_100pct() -> None:
    count, max_weight, skip = evaluate_book(
        [HoldingRow(symbol="688425.SH", weight=1.0, action="hold")]
    )
    assert count == 1
    assert max_weight == pytest.approx(1.0)
    assert skip is not None
    assert "残仓" in skip
    assert "1 只" in skip


def test_evaluate_book_accepts_equal_weight_top6() -> None:
    count, max_weight, skip = evaluate_book(
        _eq_holdings(["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ", "F.SZ"])
    )
    assert count == 6
    assert skip is None
    assert max_weight == pytest.approx(1.0 / 6)


def test_evaluate_book_rejects_two_names_even_if_split() -> None:
    count, _, skip = evaluate_book(_eq_holdings(["AAA.SZ", "BBB.SZ"]))
    assert count == 2
    assert skip is not None


def test_stamp_book_and_first_tradable() -> None:
    leftover = SectorScore(
        key="machine", name="工业母机", panel="x", run_id="r1", variant="f0_prime", rank=1
    )
    healthy = SectorScore(
        key="speceq", name="其他专用设备", panel="x", run_id="r2", variant="f0_prime", rank=2
    )
    assert stamp_book(leftover, [HoldingRow(symbol="688425.SH", weight=1.0)]) is False
    assert leftover.book_healthy is False
    assert leftover.holding_count == 1
    assert stamp_book(healthy, _eq_holdings(["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ", "F.SZ"])) is True
    assert first_tradable([leftover, healthy]) is healthy


def test_build_reason_mentions_skipped_index_leader() -> None:
    leader = SectorScore(
        key="machine", name="工业母机", panel="x", run_id="r1", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.21, confirm_pass_rate=0.6,
        skip_reason="策略残仓：持仓 1 只，单票 100%（需至少 3 只且单票≤50%）",
    )
    chosen = SectorScore(
        key="speceq", name="其他专用设备", panel="x", run_id="r2", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.208, confirm_pass_rate=0.6,
    )
    text = build_reason(chosen, "full", index_leader=leader)
    assert text.startswith("其他专用设备：")
    assert "工业母机" in text
    assert "已跳过" in text


def test_build_untradable_reason_uses_leader_skip() -> None:
    leader = SectorScore(
        key="machine", name="工业母机", panel="x", run_id="r1", variant="f0_prime",
        skip_reason="策略残仓：持仓 1 只，单票 100%（需至少 3 只且单票≤50%）",
    )
    text = build_untradable_reason(leader)
    assert "没有可执行账本" in text
    assert "工业母机" in text


def test_pick_tradable_skips_residual_index_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leftover = SectorScore(
        key="machine", name="工业母机", panel="x",
        run_id="20260726_084500_03_F0_machine_171_054", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.21, rank=1,
    )
    healthy = SectorScore(
        key="speceq", name="其他专用设备", panel="x",
        run_id="20260730_162800_03_F0_speceq_127_111", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.208, rank=2,
    )

    def _fake_load(recipe, **kwargs):
        if recipe.key == "machine":
            return (
                [HoldingRow(symbol="688425.SH", weight=1.0, action="hold")],
                DayOps(),
                False,
            )
        names = ["002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"]
        return (_eq_holdings(names), DayOps(), False)

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _fake_load)
    chosen, holdings, _ops, _prov = _pick_tradable_book(
        [leftover, healthy],
        as_of_date=date(2026, 8, 18),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is not None
    assert chosen.key == "speceq"
    assert leftover.book_healthy is False
    assert leftover.holding_count == 1
    assert leftover.max_weight == pytest.approx(1.0)
    assert healthy.book_healthy is True
    assert [row.symbol for row in holdings] == [
        "002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"
    ]


def test_pick_tradable_none_when_all_residual(monkeypatch: pytest.MonkeyPatch) -> None:
    robot = SectorScore(
        key="robot", name="人形机器人", panel="x",
        run_id="20260725_184000_05_F0_robot_124_178", variant="f0_prime", rank=1,
    )
    machine = SectorScore(
        key="machine", name="工业母机", panel="x",
        run_id="20260726_084500_03_F0_machine_171_054", variant="f0_prime", rank=2,
    )

    def _fake_load(recipe, **kwargs):
        symbol = "300256.SZ" if recipe.key == "robot" else "688425.SH"
        return ([HoldingRow(symbol=symbol, weight=1.0, action="hold")], DayOps(), False)

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _fake_load)
    chosen, holdings, _ops, _prov = _pick_tradable_book(
        [robot, machine],
        as_of_date=date(2026, 8, 18),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is None
    assert holdings == []
    assert robot.book_healthy is False
    assert machine.book_healthy is False


def test_locked_healthy_book_is_kept_when_map_leader_flips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copper = SectorScore(
        key="copper", name="金属铜", panel="x",
        run_id="20260730_000000_03_F0_copper_081_160", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, live_score=1.0, rank=2,
    )
    robot = SectorScore(
        key="robot", name="人形机器人", panel="x",
        run_id="20260725_184000_05_F0_robot_124_178", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.18, live_score=1.5, rank=1,
    )
    names = ["002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"]

    def _fake_load(recipe, **kwargs):
        return (_eq_holdings(names), DayOps(), False)

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _fake_load)
    chosen, holdings, _ops, _prov, lock, event, _reason = _select_locked_book(
        [robot, copper],
        lock=ThemeLock(locked_key="copper"),
        map_row=robot,
        as_of="2026-08-18",
        as_of_date=date(2026, 8, 18),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is not None and chosen.key == "copper"
    assert event == "held"
    assert lock.locked_key == "copper"
    assert lock.challenger_key == "robot"
    assert lock.challenger_days == 1
    assert [row.symbol for row in holdings] == names


def test_challenger_steals_lock_after_confirm_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copper = SectorScore(
        key="copper", name="金属铜", panel="x",
        run_id="run_copper", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, live_score=1.0, rank=2,
    )
    robot = SectorScore(
        key="robot", name="人形机器人", panel="x",
        run_id="run_robot", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.18, live_score=1.5, rank=1,
    )
    names = ["002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"]

    def _fake_load(recipe, **kwargs):
        return (_eq_holdings(names), DayOps(), False)

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _fake_load)
    incoming = ThemeLock(
        locked_key="copper",
        challenger_key="robot",
        challenger_days=1,
        challenger_as_of="2026-08-18",
    )
    chosen, _holdings, _ops, _prov, lock, event, _reason = _select_locked_book(
        [robot, copper],
        lock=incoming,
        map_row=robot,
        as_of="2026-08-19",
        as_of_date=date(2026, 8, 19),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is not None and chosen.key == "robot"
    assert event == "switched_hysteresis"
    assert lock.locked_key == "robot"
    assert lock.challenger_key is None


def test_same_day_rescan_does_not_count_as_second_challenger_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copper = SectorScore(
        key="copper", name="金属铜", panel="x",
        run_id="run_copper", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.10, live_score=1.0, rank=2,
    )
    robot = SectorScore(
        key="robot", name="人形机器人", panel="x",
        run_id="run_robot", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.18, live_score=1.5, rank=1,
    )
    names = ["002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"]
    monkeypatch.setattr(
        "src.sector_pick.scan._load_holdings",
        lambda recipe, **kwargs: (_eq_holdings(names), DayOps(), False),
    )
    incoming = ThemeLock(
        locked_key="copper",
        challenger_key="robot",
        challenger_days=1,
        challenger_as_of="2026-08-18",
    )
    chosen, _holdings, _ops, _prov, lock, event, _reason = _select_locked_book(
        [robot, copper],
        lock=incoming,
        map_row=robot,
        as_of="2026-08-18",
        as_of_date=date(2026, 8, 18),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is not None and chosen.key == "copper"
    assert event == "held"
    assert lock.challenger_days == 1


def test_bear_scan_ticks_challenger_and_steals_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_score(recipe, **_kwargs):
        if recipe.key == "vrar":
            return SectorScore(
                key="vrar",
                name="虚拟现实",
                panel="x",
                run_id=recipe.run_id,
                variant=recipe.variant,
                enabled=True,
                timing_mult=1.0,
                ret_20d=0.03,
                confirm_pass_rate=0.6,
                ew_ma20_ratio=1.01,
            )
        return SectorScore(
            key=recipe.key,
            name=recipe.name,
            panel="x",
            run_id=recipe.run_id,
            variant=recipe.variant,
            enabled=True,
            timing_mult=0.5,
            ret_20d=0.01,
            confirm_pass_rate=0.6,
            ew_ma20_ratio=0.99,
        )

    monkeypatch.setattr("src.sector_pick.scan.score_recipe", _fake_score)
    monkeypatch.setattr(
        "src.sector_pick.scan._fetch_csi300_close",
        lambda **_k: (pd.Series([1.0]), "bear", -0.05),
    )
    monkeypatch.setattr(
        "src.sector_pick.scan.horizons_from_close",
        lambda _close: {"short": "bear", "medium": "bear", "long": "chop"},
    )
    def _should_not_load(*_a, **_k):
        raise AssertionError("bear path must not open a holdings book")

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _should_not_load)

    first = run_sector_pick_scan(
        ["vrar", "speceq"],
        as_of=date(2026, 9, 7),
        theme_lock=ThemeLock(locked_key="speceq"),
    )
    assert first.map_key == "vrar"
    assert first.locked_key == "speceq"
    assert first.recommended_key == "speceq"
    assert first.challenger_key == "vrar"
    assert first.challenger_days == 1
    assert first.lock_event == "held_bear"
    assert first.stance == "none"
    assert first.holdings == []

    stolen = run_sector_pick_scan(
        ["vrar", "speceq"],
        as_of=date(2026, 9, 8),
        theme_lock=ThemeLock(
            locked_key="speceq",
            challenger_key="vrar",
            challenger_days=1,
            challenger_as_of="2026-09-07",
        ),
    )
    assert stolen.map_key == "vrar"
    assert stolen.locked_key == "vrar"
    assert stolen.recommended_key == "vrar"
    assert stolen.challenger_days == 0
    assert stolen.lock_event == "switched_hysteresis"
    assert stolen.stance == "none"
    assert stolen.holdings == []
    assert "虚拟现实" in stolen.reason


def test_lock_switches_on_residual_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leftover = SectorScore(
        key="machine", name="工业母机", panel="x",
        run_id="20260726_084500_03_F0_machine_171_054", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.21, live_score=1.6, rank=1,
    )
    healthy = SectorScore(
        key="speceq", name="其他专用设备", panel="x",
        run_id="20260730_162800_03_F0_speceq_127_111", variant="f0_prime",
        timing_mult=1.0, ret_20d=0.208, live_score=1.4, rank=2,
    )

    def _fake_load(recipe, **kwargs):
        if recipe.key == "machine":
            return (
                [HoldingRow(symbol="688425.SH", weight=1.0, action="hold")],
                DayOps(),
                False,
            )
        names = ["002073.SZ", "002337.SZ", "002529.SZ", "002786.SZ", "300412.SZ", "603855.SH"]
        return (_eq_holdings(names), DayOps(), False)

    monkeypatch.setattr("src.sector_pick.scan._load_holdings", _fake_load)
    chosen, holdings, _ops, _prov, lock, event, _reason = _select_locked_book(
        [leftover, healthy],
        lock=ThemeLock(locked_key="machine"),
        map_row=leftover,
        as_of="2026-08-18",
        as_of_date=date(2026, 8, 18),
        lookback_days=400,
        runs_root=Path("."),
    )
    assert chosen is not None and chosen.key == "speceq"
    assert event == "switched_failure"
    assert lock.locked_key == "speceq"
    assert leftover.book_healthy is False
    assert [row.symbol for row in holdings][0] == "002073.SZ"


def test_score_roundtrip_keeps_book_gate_fields() -> None:
    row = SectorScore(
        key="machine", name="工业母机", panel="BK1004",
        run_id="run_x", variant="f0_prime", rank=1,
        holding_count=1, max_weight=1.0, book_healthy=False,
        skip_reason="策略残仓：持仓 1 只，单票 100%（需至少 3 只且单票≤50%）",
    )
    loaded = SectorScore.from_dict(row.to_dict())
    assert loaded is not None
    assert loaded.book_healthy is False
    assert loaded.holding_count == 1
    assert loaded.max_weight == pytest.approx(1.0)
    assert loaded.skip_reason is not None and "残仓" in loaded.skip_reason


def test_score_recipe_uses_injected_panel() -> None:
    recipe = SectorRecipe(
        key="toy",
        name="Toy",
        panel="Toy",
        confirm_ids=(),
        use_max_veto=False,
        run_id="missing_run",
        prop_file="x.md",
        variant="f0_prime",
    )
    close = _close_trend()
    data_map = {
        "AAA.SZ": pd.DataFrame({"close": close["AAA.SZ"]}),
        "BBB.SZ": pd.DataFrame({"close": close["BBB.SZ"]}),
    }
    row = score_recipe(
        recipe,
        as_of_date=date(2024, 3, 1),
        lookback_days=400,
        runs_root=Path("."),
        irs={},
        data_map=data_map,
    )
    assert row.error is None
    assert row.timing_mult == 1.0
    assert row.confirm_pass_rate == 1.0  # no confirm legs → everyone passes
    assert row.ret_20d is not None


def test_close_panel_skips_empty_frames() -> None:
    idx = pd.bdate_range("2024-01-02", periods=5)
    panel = close_panel(
        {
            "AAA.SZ": pd.DataFrame({"close": [1, 2, 3, 4, 5]}, index=idx),
            "BAD.SZ": pd.DataFrame(),
        }
    )
    assert list(panel.columns) == ["AAA.SZ"]
    assert len(panel) == 5


def test_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state = load_state()
    assert state.enabled_keys == default_enabled_keys()
    state.enabled_keys = ["robot", "copper"]
    save_state(state)
    loaded = load_state()
    assert loaded.enabled_keys == ["robot", "copper"]


def test_store_keeps_explicit_empty_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state = load_state()
    state.enabled_keys = []
    save_state(state)
    loaded = load_state()
    assert loaded.enabled_keys == []


def _result(as_of: str, symbols: list[str], scanned_at: str = "") -> SectorPickResult:
    holdings = [
        HoldingRow(symbol=code, weight=1.0 / len(symbols), action="hold")
        for code in symbols
    ]
    return SectorPickResult(
        as_of=as_of,
        scanned_at=scanned_at or f"{as_of}T08:00:00+00:00",
        recommended_key="robot",
        holdings_key="robot",
        stance="full",
        reason="test",
        holdings=holdings,
        ops=DayOps(),
    )


def test_same_day_scan_overwrites_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    first = commit_scan_result(_result("2024-01-02", ["AAA.SZ"], "2024-01-02T01:00:00+00:00"))
    assert first.ops.entered == ["AAA.SZ"]
    second = commit_scan_result(_result("2024-01-02", ["BBB.SZ"], "2024-01-02T06:00:00+00:00"))
    loaded = load_day_snapshot("2024-01-02")
    assert loaded is not None
    assert loaded.scanned_at == "2024-01-02T06:00:00+00:00"
    assert [row.symbol for row in loaded.holdings] == ["BBB.SZ"]
    # Same-day overwrite still baselines against *prior* day (none here) so BBB is enter.
    assert second.ops.entered == ["BBB.SZ"]
    assert list_snapshot_summaries()[0]["holding_count"] == 1


def test_ops_diff_against_previous_calendar_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    commit_scan_result(_result("2024-01-02", ["AAA.SZ"]))
    next_day = commit_scan_result(_result("2024-01-03", ["AAA.SZ", "BBB.SZ"]))
    assert sorted(next_day.ops.entered) == ["BBB.SZ"]
    assert sorted(next_day.ops.held) == ["AAA.SZ"]
    assert next_day.ops.exited == []
    later = commit_scan_result(_result("2024-01-03", ["BBB.SZ"]))
    # Same-day rescan still diffs against Jan 2 (AAA), not the earlier Jan 3 book.
    assert later.ops.entered == ["BBB.SZ"]
    assert later.ops.exited == ["AAA.SZ"]
    assert later.ops.held == []
    loaded = load_day_snapshot("2024-01-03")
    assert loaded is not None
    assert [row.symbol for row in loaded.holdings] == ["BBB.SZ"]


def test_commit_scan_keeps_holding_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ops rebase rebuilds rows; dual-track ``trend`` must survive that rewrite."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = _result("2024-01-02", ["AAA.SZ", "BBB.SZ"])
    result.holdings[0].trend = {
        "action": "reduce",
        "market_regime": "chop",
        "stock_regime": "bear",
        "swing": "mid",
        "fade_rallies": True,
        "reason": "knife",
    }
    result.holdings[1].trend = {
        "action": "hold",
        "market_regime": "chop",
        "stock_regime": "bull",
        "swing": "mid",
        "fade_rallies": False,
        "reason": "rs",
    }
    committed = commit_scan_result(result)
    by_symbol = {row.symbol: row.trend for row in committed.holdings}
    assert by_symbol["AAA.SZ"] is not None and by_symbol["AAA.SZ"]["action"] == "reduce"
    assert by_symbol["BBB.SZ"] is not None and by_symbol["BBB.SZ"]["action"] == "hold"
    # Hold ranks before reduce even though rebase originally sorts by weight.
    assert [row.symbol for row in committed.holdings] == ["BBB.SZ", "AAA.SZ"]
    loaded = load_day_snapshot("2024-01-02")
    assert loaded is not None
    assert loaded.holdings[0].trend is not None
    assert loaded.holdings[0].trend["action"] == "hold"


def test_history_series_is_chronological_with_ranks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _with_ranks(as_of: str, winner: str, ranks: dict[str, int]) -> SectorPickResult:
        result = _result(as_of, ["AAA.SZ"])
        result.recommended_key = winner
        result.sectors = [
            SectorScore(
                key=key,
                name=key,
                panel=key,
                run_id=f"run_{key}",
                variant="f0_prime",
                rank=rank,
                ret_20d=0.05 if rank == 1 else -0.02,
                confirm_pass_rate=0.6,
                timing_mult=1.0 if rank == 1 else 0.5,
            )
            for key, rank in ranks.items()
        ]
        return result

    commit_scan_result(_with_ranks("2024-01-02", "robot", {"robot": 1, "copper": 2}))
    commit_scan_result(_with_ranks("2024-01-03", "copper", {"robot": 2, "copper": 1}))
    series = list_history_series()
    dates = [day["as_of"] for day in series["days"]]
    assert dates == ["2024-01-02", "2024-01-03"]
    first = {row["key"]: row["rank"] for row in series["days"][0]["sectors"]}
    second = {row["key"]: row["rank"] for row in series["days"][1]["sectors"]}
    assert first == {"robot": 1, "copper": 2}
    assert second == {"robot": 2, "copper": 1}
    assert series["days"][1]["recommended_key"] == "copper"
