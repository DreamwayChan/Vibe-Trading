"""Session assembly around the fill rules: bars, lots, guards, run resolution.

``test_paper_book.py`` covers the end-to-end day steps. These cases cover the
pieces a step is built from — how a lake frame becomes a bar, how lots are
consumed, which run directories may be read, and which bad inputs are refused
instead of defaulted.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import src.api.helpers
import src.paper_book.book as book
from backtest.engines.china_a import ChinaAEngine
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.paper_book.book import (
    _bar_on,
    _buy_intent,
    _consume_lots,
    _default_book_id,
    _fraction,
    _initial_cash,
    _mark_positions,
    _present,
    _sell_intent,
    _session_bar,
    _threshold,
    load_day_bars,
    resolve_run_dir,
    step_day,
)
from src.paper_book.models import BookState, Intent, Lot, PositionState
from src.paper_book.store import PaperBookError, load_state, paper_book_root, save_state

DAY = "2026-01-05"
PREV = "2026-01-02"
SYMBOL = "000001.SZ"


def _frame(rows: dict[str, dict]) -> pd.DataFrame:
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index = pd.to_datetime(frame.index)
    return frame


# --------------------------------------------------------------------------
# lake frame -> bar
# --------------------------------------------------------------------------


def test_a_bar_without_pre_close_borrows_the_previous_session_close() -> None:
    """The limit heuristic needs yesterday's close; today's would always be 0%."""
    frame = _frame(
        {
            PREV: {"open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0, "volume": 1e3},
            DAY: {"open": 10.5, "high": 11.0, "low": 10.4, "close": 11.0, "volume": 2e3},
        }
    )
    bar = _bar_on(frame, DAY)
    assert bar is not None
    assert bar["pre_close"] == 10.0
    assert bar["close"] == 11.0
    assert bar["trade_date"] == pd.Timestamp(DAY)


def test_an_explicit_pre_close_is_not_overwritten() -> None:
    frame = _frame(
        {
            PREV: {"close": 10.0},
            DAY: {"close": 11.0, "pre_close": 10.9},
        }
    )
    assert _bar_on(frame, DAY)["pre_close"] == 10.9


def test_a_first_session_gets_no_invented_pre_close() -> None:
    """With no earlier row there is nothing to compare against — leave it out."""
    bar = _bar_on(_frame({DAY: {"close": 11.0}}), DAY)
    assert bar is not None
    assert "pre_close" not in bar.index


def test_a_zero_pre_close_is_treated_as_missing() -> None:
    frame = _frame({PREV: {"close": 10.0}, DAY: {"close": 11.0, "pre_close": 0.0}})
    assert _bar_on(frame, DAY)["pre_close"] == 10.0


def test_an_unrequested_day_has_no_bar() -> None:
    frame = _frame({PREV: {"close": 10.0}})
    assert _bar_on(frame, DAY) is None
    assert _bar_on(pd.DataFrame(), DAY) is None


def test_a_duplicated_session_keeps_the_last_row() -> None:
    """Ingestion can write a corrected bar for the same day; the correction wins."""
    frame = _frame({DAY: {"close": 11.0}, PREV: {"close": 10.0}})
    frame = pd.concat([frame, _frame({DAY: {"close": 12.0}})]).sort_index()
    assert _bar_on(frame, DAY)["close"] == 12.0


def test_a_bar_without_a_session_stamp_gets_one() -> None:
    stamped = _session_bar(pd.Series({"close": 10.0, "volume": 100.0}), DAY)
    assert stamped["trade_date"] == pd.Timestamp(DAY)
    # The original series is left alone: callers may reuse it.
    original = pd.Series({"close": 10.0, "trade_date": pd.Timestamp(DAY)})
    assert _session_bar(original, DAY) is original


def test_a_nat_session_stamp_is_replaced() -> None:
    bar = pd.Series({"close": 10.0, "trade_date": pd.NaT})
    assert _session_bar(bar, DAY)["trade_date"] == pd.Timestamp(DAY)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), (pd.NaT, False), (float("nan"), False), (0, True), ("", True)],
)
def test_present_rejects_only_missing_values(value: object, expected: bool) -> None:
    assert _present(value) is expected


def test_present_survives_a_value_pandas_cannot_judge() -> None:
    """Cell values can arrive as arrays; ``if pd.isna(...)`` then raises.

    Returning True keeps the row, which is the safe direction: a present value
    that cannot be judged must not be silently treated as missing.
    """
    import numpy as np

    assert _present(np.array([1.0, 2.0])) is True


def test_an_unparseable_pre_close_falls_back_to_the_previous_close() -> None:
    frame = _frame({PREV: {"close": 10.0}, DAY: {"close": 11.0, "pre_close": "n/a"}})
    assert _bar_on(frame, DAY)["pre_close"] == 10.0


# --------------------------------------------------------------------------
# lots
# --------------------------------------------------------------------------


def _position(*lots: tuple[int, str]) -> PositionState:
    return PositionState(
        symbol=SYMBOL,
        lots=[Lot(shares=shares, buy_date=day, price=10.0) for shares, day in lots],
        last_price=10.0,
    )


def test_lots_are_consumed_oldest_first() -> None:
    position = _position((100, "2026-01-02"), (200, "2026-01-03"))
    _consume_lots(position, 150, DAY, t_plus_one=True)
    assert [(lot.shares, lot.buy_date) for lot in position.lots] == [(150, "2026-01-03")]


def test_a_t_plus_one_lot_is_skipped_and_kept() -> None:
    """Selling the older lot must not touch shares bought this session."""
    position = _position((100, DAY), (300, PREV))
    _consume_lots(position, 300, DAY, t_plus_one=True)
    assert [(lot.shares, lot.buy_date) for lot in position.lots] == [(100, DAY)]


def test_disabling_t_plus_one_lets_the_same_day_lot_go() -> None:
    position = _position((100, DAY))
    _consume_lots(position, 100, DAY, t_plus_one=False)
    assert position.lots == []


def test_consuming_more_than_is_held_empties_the_position() -> None:
    position = _position((100, PREV))
    _consume_lots(position, 500, DAY, t_plus_one=True)
    assert position.shares == 0


# --------------------------------------------------------------------------
# marks and guards
# --------------------------------------------------------------------------


def test_a_name_without_a_bar_keeps_its_last_mark() -> None:
    """A halt must not mark to zero and book a fake 100% drawdown."""
    position = _position((100, PREV))
    state = BookState(book_id="b", positions={SYMBOL: position})
    _mark_positions(state, {})
    assert position.last_price == 10.0


def test_a_bar_without_a_price_keeps_the_previous_mark() -> None:
    position = _position((100, PREV))
    state = BookState(book_id="b", positions={SYMBOL: position})
    _mark_positions(state, {SYMBOL: pd.Series({"close": 0.0})})
    assert position.last_price == 10.0

    _mark_positions(state, {SYMBOL: pd.Series({"close": 12.5})})
    assert position.last_price == 12.5


def _config(*, run_id: str = "run1", shadow_id: str = "") -> MonitorConfig:
    return MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=run_id, shadow_id=shadow_id),
        suggestions=[
            MonitorSuggestion(symbol=SYMBOL, action="buy", reason="s", as_of=DAY, tradable=True)
        ],
        tradable=True,
    )


def test_the_book_id_prefers_the_run_then_the_shadow_then_the_default() -> None:
    assert _default_book_id(_config(run_id="run1")) == "run1"
    assert _default_book_id(_config(run_id=" ", shadow_id="s1")) == "shadow_s1"
    assert _default_book_id(_config(run_id="", shadow_id="")) == "default"


@pytest.mark.parametrize("value", [-0.1, float("nan")])
def test_a_negative_or_nan_deviation_threshold_is_refused(value: float) -> None:
    """A NaN threshold compares False against everything — a silent mute."""
    with pytest.raises(PaperBookError, match="deviation threshold"):
        _threshold(value)


def test_a_zero_threshold_is_allowed() -> None:
    assert _threshold(0.0) == 0.0


@pytest.mark.parametrize("value", [0.0, -0.5, 1.5, float("nan")])
def test_position_and_drawdown_caps_must_be_fractions(value: float) -> None:
    with pytest.raises(PaperBookError, match="must be in \\(0, 1\\]"):
        _fraction("max position", value)


@pytest.mark.parametrize("value", [1.0, 0.2])
def test_a_cap_of_one_is_still_a_fraction(value: float) -> None:
    assert _fraction("max position", value) == value


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_initial_cash_must_be_positive(value: float) -> None:
    with pytest.raises(PaperBookError, match="initial cash"):
        _initial_cash(value)


# --------------------------------------------------------------------------
# run directory resolution
# --------------------------------------------------------------------------


def test_a_run_key_cannot_escape_the_runs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound backtest path comes from a stored run id, so it must be contained."""
    runs = tmp_path / "runs"
    (runs / "run1").mkdir(parents=True)
    monkeypatch.setattr(src.api.helpers, "RUNS_DIR", runs)

    assert resolve_run_dir("../secrets") is None
    assert resolve_run_dir("run1/../../secrets") is None
    assert resolve_run_dir("") is None


def test_a_missing_run_directory_resolves_to_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "not-a-dir").write_text("x", encoding="utf-8")
    monkeypatch.setattr(src.api.helpers, "RUNS_DIR", runs)

    assert resolve_run_dir("not-a-dir") is None
    assert resolve_run_dir("absent") is None


def test_a_real_run_directory_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    (runs / "run1").mkdir(parents=True)
    monkeypatch.setattr(src.api.helpers, "RUNS_DIR", runs)
    assert resolve_run_dir("run1") == (runs / "run1").resolve()


# --------------------------------------------------------------------------
# load_day_bars
# --------------------------------------------------------------------------


def test_no_symbols_means_no_lake_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.data.lake_fetch.read_lake", lambda *a, **k: pytest.fail("must not read")
    )
    assert load_day_bars([], DAY) == {}


def test_a_broken_lake_is_an_empty_book_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The book must still settle (and report missing bars) if the lake is unreadable."""

    def _boom(*args, **kwargs):
        raise OSError("lake offline")

    monkeypatch.setattr("src.data.lake_fetch.read_lake", _boom)
    assert load_day_bars([SYMBOL], DAY) == {}


def test_only_the_requested_session_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = _frame(
        {
            PREV: {"open": 9.8, "close": 10.0, "volume": 1e3},
            DAY: {"open": 10.0, "close": 10.5, "volume": 2e3},
        }
    )

    class _Stored:
        frames = {"000001.sz": frame, "600519.SH": _frame({PREV: {"close": 1600.0}})}

    monkeypatch.setattr("src.data.lake_fetch.read_lake", lambda *a, **k: _Stored())
    bars = load_day_bars(["000001.SZ", "600519.SH"], DAY)

    assert list(bars) == ["000001.SZ"]
    assert bars["000001.SZ"]["close"] == 10.5
    assert bars["000001.SZ"]["pre_close"] == 10.0


# --------------------------------------------------------------------------
# intent handlers
# --------------------------------------------------------------------------


def _intent(*, action: str, weight: float) -> Intent:
    return Intent(symbol=SYMBOL, action=action, target_weight=weight)


def test_a_zero_weight_buy_is_not_an_unfilled_row() -> None:
    fill, row = _buy_intent(
        BookState(book_id="b"),
        day=DAY,
        intent=_intent(action="buy", weight=0.0),
        bars={},
        engine=ChinaAEngine({}),
        nav=1_000_000.0,
        max_volume_ratio=0.0,
    )
    assert (fill, row) == (None, None)


def test_a_buy_without_a_bar_is_reported_as_missing() -> None:
    fill, row = _buy_intent(
        BookState(book_id="b"),
        day=DAY,
        intent=_intent(action="buy", weight=0.1),
        bars={},
        engine=ChinaAEngine({}),
        nav=1_000_000.0,
        max_volume_ratio=0.0,
    )
    assert fill is None
    assert row is not None and row.reason == "missing_bar"


def test_a_buy_without_nav_cannot_size_a_position() -> None:
    fill, row = _buy_intent(
        BookState(book_id="b", cash=0.0),
        day=DAY,
        intent=_intent(action="buy", weight=0.1),
        bars={SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})},
        engine=ChinaAEngine({}),
        nav=0.0,
        max_volume_ratio=0.0,
    )
    assert fill is None
    assert row is not None and row.reason == "bad_price"


def test_selling_a_name_that_is_not_held_does_nothing() -> None:
    fill, rows = _sell_intent(
        BookState(book_id="b"),
        day=DAY,
        intent=_intent(action="sell", weight=0.0),
        bars={SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})},
        engine=ChinaAEngine({}),
    )
    assert (fill, rows) == (None, [])


def test_selling_without_a_bar_reports_the_held_quantity() -> None:
    state = BookState(book_id="b", positions={SYMBOL: _position((300, PREV))})
    fill, rows = _sell_intent(
        state,
        day=DAY,
        intent=_intent(action="sell", weight=0.0),
        bars={},
        engine=ChinaAEngine({}),
    )
    assert fill is None
    assert [row.reason for row in rows] == ["missing_bar"]
    assert rows[0].shares == 300


def test_a_full_sell_closes_the_position_and_credits_cash() -> None:
    state = BookState(book_id="b", cash=0.0, positions={SYMBOL: _position((300, PREV))})
    fill, rows = _sell_intent(
        state,
        day=DAY,
        intent=_intent(action="sell", weight=0.0),
        bars={SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})},
        engine=ChinaAEngine({}),
    )
    assert fill is not None and rows == []
    assert SYMBOL not in state.positions
    assert state.cash == pytest.approx(fill.notional - fill.commission)
    assert state.cash > 0


# --------------------------------------------------------------------------
# step_day guards
# --------------------------------------------------------------------------


def _step(tmp_path: Path, **kwargs):
    return step_day(
        as_of=kwargs.pop("as_of", DAY),
        config=kwargs.pop("config", _config()),
        root=kwargs.pop("root", tmp_path / "book"),
        bars=kwargs.pop("bars", {}),
        engine=kwargs.pop("engine", ChinaAEngine({})),
        threshold=kwargs.pop("threshold", 1.0),
        initial_cash=kwargs.pop("initial_cash", 1_000_000.0),
        **kwargs,
    )


def test_restepping_without_a_checkpoint_is_refused(tmp_path: Path) -> None:
    """A session that claims to be settled but kept no checkpoint cannot replay."""
    root = paper_book_root(tmp_path / "book")
    save_state(
        root,
        BookState(book_id="run1", run_id="run1", settled_through=DAY, opening=None),
    )

    with pytest.raises(PaperBookError, match="opening checkpoint"):
        _step(tmp_path)


def test_a_stored_book_picks_up_the_run_id_from_the_config(tmp_path: Path) -> None:
    root = paper_book_root(tmp_path / "book")
    save_state(root, BookState(book_id="run1", run_id="", cash=1_000_000.0))

    _step(tmp_path)

    assert load_state(root, "run1").run_id == "run1"


def test_a_deviation_downgrade_works_without_an_injected_store(tmp_path: Path) -> None:
    """The default path opens the strategy store itself; a failure there is logged."""
    run_dir = tmp_path / "runs" / "run1"
    (run_dir / "artifacts").mkdir(parents=True)
    pd.DataFrame(
        [
            {"timestamp": PREV, "equity": 1_000_000.0},
            {"timestamp": DAY, "equity": 1_000_000.0},
        ]
    ).to_csv(run_dir / "artifacts" / "equity.csv", index=False)

    report = _step(
        tmp_path,
        bars={SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})},
        run_dir=run_dir,
        threshold=0.0,
    )
    assert report.downgraded is True


def test_an_unopenable_strategy_store_does_not_drop_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(book, "_open_store", lambda: None)
    run_dir = tmp_path / "runs" / "run1"
    (run_dir / "artifacts").mkdir(parents=True)
    pd.DataFrame(
        [
            {"timestamp": PREV, "equity": 1_000_000.0},
            {"timestamp": DAY, "equity": 1_000_000.0},
        ]
    ).to_csv(run_dir / "artifacts" / "equity.csv", index=False)

    report = _step(
        tmp_path,
        bars={SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})},
        run_dir=run_dir,
        threshold=0.0,
    )
    assert report.downgraded is True


def test_open_store_turns_an_import_or_io_failure_into_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.monitor.admission as admission

    monkeypatch.setattr(admission, "open_strategy_store", _raise_store)
    assert book._open_store() is None


def _raise_store():
    raise OSError("store unavailable")


def test_a_report_written_before_the_drawdown_flag_still_replays(tmp_path: Path) -> None:
    """Old reports have a risk block without ``drawdown_blocked``.

    A restep of such a session must recompute the block from the drawdown rather
    than defaulting to "not blocked", which would resume buying into a drawdown.
    """
    import json

    root = paper_book_root(tmp_path / "book")
    bars = {SYMBOL: pd.Series({"open": 10.0, "close": 10.0, "volume": 1e3})}
    first = _step(tmp_path, bars=bars, threshold=1.0)
    assert first.risk["drawdown_blocked"] is False

    path = root / "run1" / "reports" / f"{DAY}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["risk"].pop("drawdown_blocked")
    path.write_text(json.dumps(payload), encoding="utf-8")

    replay = _step(tmp_path, bars=bars, threshold=1.0)

    assert replay.risk["drawdown_blocked"] is False
    assert replay.risk["drawdown"] == pytest.approx(0.0)
    assert [fill.shares for fill in replay.fills] == [fill.shares for fill in first.fills]
