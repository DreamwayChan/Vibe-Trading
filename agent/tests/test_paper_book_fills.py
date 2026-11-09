"""Order-level fill rules: cash safety, block reasons, and lot accounting.

``test_paper_book.py`` drives whole sessions. These cases aim at the single
order: what happens when the gap is under one lot, when the panel contradicts
the bar, when a sell is half locked by T+1, and whether any of it leaves state
behind in the engine.
"""

from __future__ import annotations

import pandas as pd
import pytest

import src.paper_book.fills as fills
from backtest.engines.china_a import ChinaAEngine
from src.data.tradability import TradabilityIndex
from src.paper_book.fills import (
    _float_or_none,
    affordable_buy,
    explain_block,
    make_engine,
    mark_price,
    ref_price,
    try_buy,
    try_sell,
)

DAY = "2026-01-05"
SYMBOL = "000001.SZ"


def _bar(**extra) -> pd.Series:
    data = {
        "open": 10.0,
        "close": 10.0,
        "pre_close": 10.0,
        "volume": 1_000.0,
        "trade_date": pd.Timestamp(DAY),
    }
    data.update(extra)
    return pd.Series(data)


def _panel(**rows) -> TradabilityIndex:
    """One-row tradability panel keyed on ``SYMBOL``/``DAY``."""
    record = {"symbol": SYMBOL, "date": DAY}
    record.update(rows)
    return TradabilityIndex(pd.DataFrame([record]))


def _engine(**config) -> ChinaAEngine:
    return ChinaAEngine(dict(config))


# --------------------------------------------------------------------------
# price readers
# --------------------------------------------------------------------------


def test_a_zero_open_falls_back_to_the_close() -> None:
    """A halted open of 0 is not a price; the close still is."""
    assert ref_price(_bar(open=0.0, close=10.4)) == 10.4


def test_ref_price_ignores_columns_it_cannot_read() -> None:
    assert ref_price(pd.Series({"open": None, "close": 9.5})) == 9.5
    assert ref_price(pd.Series({"close": 9.5})) == 9.5


def test_ref_price_gives_up_when_no_usable_price_exists() -> None:
    assert ref_price(pd.Series({"open": 0.0, "close": -1.0})) is None
    assert ref_price(pd.Series({"open": None, "close": float("nan")})) is None
    assert ref_price(pd.Series({"open": "n/a", "close": None})) is None


def test_mark_price_refuses_a_non_positive_close() -> None:
    assert mark_price(_bar(close=0.0)) is None
    assert mark_price(pd.Series({"close": None})) is None
    assert mark_price(_bar(close=12.3)) == 12.3


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (float("nan"), None), ("", None), ("abc", None), (7, 7.0), ("8.5", 8.5)],
)
def test_float_or_none_only_accepts_real_numbers(value: object, expected: float | None) -> None:
    assert _float_or_none(value) == expected


# --------------------------------------------------------------------------
# affordable_buy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cash", [300.0, 1_006.0, 2_500.0, 10_000.0, 250_000.0])
def test_a_buy_never_spends_more_than_the_cash_available(cash: float) -> None:
    """The lot size plus its commission must fit, and one more lot must not.

    This is the invariant that keeps the paper book from going negative: the
    loop shrinks by 100 shares until the whole bill — notional *and* commission —
    is covered. Asserting maximality too, because "buy nothing" also satisfies
    the budget and would silently stall every rebalance.
    """
    engine = _engine()
    ref = 10.0
    shares, fill, commission = affordable_buy(engine, cash, ref, 3_000)

    assert shares % 100 == 0
    assert shares * fill + commission <= cash + 1e-6

    bigger = shares + 100
    bigger_commission = engine.calc_commission(bigger, fill, 1, is_open=True)
    assert bigger * fill + bigger_commission > cash + 1e-6 or shares == 3_000


def test_a_full_affordable_request_is_not_clipped() -> None:
    engine = _engine()
    shares, fill, commission = affordable_buy(engine, 1_000_000.0, 10.0, 500)
    assert shares == 500
    assert fill == pytest.approx(10.01)
    # Minimum commission plus the transfer fee on 5,005 of notional.
    assert commission == pytest.approx(5.0 + 5_005 * 0.00001)


def test_a_price_slipped_below_zero_buys_nothing() -> None:
    """Negative slippage cannot make a free lot: the guard returns 0 shares."""
    engine = _engine(slippage=-5.0)
    shares, fill, commission = affordable_buy(engine, 1_000_000.0, 10.0, 500)
    assert shares == 0
    assert fill <= 0
    assert commission == 0.0


def test_cash_for_less_than_one_lot_buys_nothing() -> None:
    engine = _engine()
    shares, fill, commission = affordable_buy(engine, 900.0, 10.0, 500)
    assert (shares, commission) == (0, 0.0)


# --------------------------------------------------------------------------
# try_buy
# --------------------------------------------------------------------------


def test_a_gap_under_one_lot_is_not_an_order_at_all() -> None:
    """Delta < 100 is silence, not a rejection.

    Reporting it as unfilled would put "insufficient cash" rows in the daily
    report for a rebalance that is simply below the board lot.
    """
    fill, row, cash = try_buy(
        _engine(), symbol=SYMBOL, bar=_bar(), cash=100_000.0, target_shares=150, held_shares=100
    )
    assert (fill, row, cash) == (None, None, 100_000.0)


def test_a_bar_with_no_usable_price_is_reported_not_guessed() -> None:
    fill, row, cash = try_buy(
        _engine(),
        symbol=SYMBOL,
        bar=pd.Series({"open": 0.0, "close": 0.0, "volume": 100.0}),
        cash=100_000.0,
        target_shares=1_000,
        held_shares=0,
    )
    assert fill is None
    assert row is not None and row.reason == "bad_price"
    assert cash == 100_000.0


def test_insufficient_cash_is_distinguished_from_a_blocked_order() -> None:
    fill, row, cash = try_buy(
        _engine(), symbol=SYMBOL, bar=_bar(), cash=500.0, target_shares=1_000, held_shares=0
    )
    assert fill is None
    assert row is not None and row.reason == "insufficient_cash"
    assert cash == 500.0


def test_a_limit_up_buy_is_unfilled_with_the_engine_reason() -> None:
    bar = _bar(open=11.0, close=11.0, pre_close=10.0)
    fill, row, cash = try_buy(
        _engine(), symbol=SYMBOL, bar=bar, cash=100_000.0, target_shares=1_000, held_shares=0
    )
    assert fill is None
    assert row is not None and row.reason == "limit_up"
    assert cash == 100_000.0


def test_a_fill_carries_the_slipped_price_and_the_adverse_move() -> None:
    fill, row, cash = try_buy(
        _engine(), symbol=SYMBOL, bar=_bar(), cash=100_000.0, target_shares=1_000, held_shares=0
    )
    assert row is None and fill is not None
    assert fill.shares == 1_000
    assert fill.ref_price == 10.0
    assert fill.fill_price == pytest.approx(10.01)
    assert fill.commission == pytest.approx(5.0 + 10_010 * 0.00001)
    assert fill.notional == pytest.approx(10_010.0)
    assert fill.slippage == pytest.approx(0.001)
    assert fill.close == 10.0
    assert cash == pytest.approx(100_000.0 - 10_010.0 - fill.commission)


# --------------------------------------------------------------------------
# try_sell
# --------------------------------------------------------------------------


def test_selling_nothing_is_not_an_error() -> None:
    fill, rows = try_sell(
        _engine(), symbol=SYMBOL, bar=_bar(), sellable=0, locked=0, entry_time=None
    )
    assert (fill, rows) == (None, [])


def test_a_fully_locked_lot_reports_t_plus_one_once() -> None:
    fill, rows = try_sell(
        _engine(),
        symbol=SYMBOL,
        bar=_bar(),
        sellable=0,
        locked=500,
        entry_time=pd.Timestamp(DAY),
    )
    assert fill is None
    assert [row.reason for row in rows] == ["t_plus_one"]
    assert rows[0].shares == 500


def test_a_partly_locked_lot_sells_the_eligible_half() -> None:
    fill, rows = try_sell(
        _engine(),
        symbol=SYMBOL,
        bar=_bar(),
        sellable=300,
        locked=200,
        entry_time=pd.Timestamp("2026-01-02"),
    )
    assert fill is not None and fill.shares == 300
    assert fill.fill_price == pytest.approx(9.99)
    assert fill.slippage == pytest.approx(0.001)
    assert [row.reason for row in rows] == ["t_plus_one"]
    assert rows[0].shares == 200


def test_a_limit_down_sell_is_refused() -> None:
    bar = _bar(open=9.0, close=9.0, pre_close=10.0)
    fill, rows = try_sell(
        _engine(),
        symbol=SYMBOL,
        bar=bar,
        sellable=500,
        locked=0,
        entry_time=pd.Timestamp("2026-01-02"),
    )
    assert fill is None
    assert [row.reason for row in rows] == ["limit_down"]


def test_a_sell_without_a_price_is_reported() -> None:
    fill, rows = try_sell(
        _engine(),
        symbol=SYMBOL,
        bar=pd.Series({"open": 0.0, "close": None, "volume": 10.0}),
        sellable=500,
        locked=0,
        entry_time=pd.Timestamp("2026-01-02"),
    )
    assert fill is None
    assert [row.reason for row in rows] == ["bad_price"]


@pytest.mark.parametrize("allowed", [True, False])
def test_the_synthetic_position_never_survives_the_check(allowed: bool) -> None:
    """``try_sell`` parks a Position in the engine to reuse ``can_execute``.

    If that row leaked, the *next* order on the same symbol would see a T+1
    position that does not exist in the book and refuse to sell real shares.
    """
    engine = _engine()
    bar = _bar() if allowed else _bar(open=9.0, close=9.0, pre_close=10.0)
    try_sell(
        engine,
        symbol=SYMBOL,
        bar=bar,
        sellable=500,
        locked=0,
        entry_time=pd.Timestamp("2026-01-02"),
    )
    assert SYMBOL not in engine.positions


def test_a_sell_reports_the_engine_reason_when_the_panel_halts_it() -> None:
    engine = _engine(tradability_index=_panel(is_suspended=True))
    fill, rows = try_sell(
        engine,
        symbol=SYMBOL,
        bar=_bar(),
        sellable=500,
        locked=0,
        entry_time=pd.Timestamp("2026-01-02"),
    )
    assert fill is None
    assert [row.reason for row in rows] == ["suspended"]


# --------------------------------------------------------------------------
# explain_block
# --------------------------------------------------------------------------


def test_same_day_entry_explains_the_t_plus_one_block() -> None:
    engine = _engine()
    engine.positions[SYMBOL] = _position(entry_time=pd.Timestamp(DAY))
    assert explain_block(engine, SYMBOL, 0, _bar(), pd.Timestamp(DAY)) == "t_plus_one"
    engine.positions.clear()


def test_the_panel_limit_up_beats_a_bar_that_looks_calm() -> None:
    """The exchange's own band is authoritative, even at a 3% move."""
    engine = _engine(tradability_index=_panel(limit_up=10.3, limit_down=9.7))
    assert explain_block(engine, SYMBOL, 1, _bar(close=10.3), None) == "limit_up"


def test_the_panel_limit_down_beats_a_bar_that_looks_calm() -> None:
    engine = _engine(tradability_index=_panel(limit_up=10.3, limit_down=9.7))
    assert explain_block(engine, SYMBOL, 0, _bar(close=9.7), None) == "limit_down"


def test_a_band_mismatch_falls_back_to_the_price_heuristic() -> None:
    """A band that contradicts the bar cannot be trusted to decide the order."""
    engine = _engine(tradability_index=_panel(limit_up=10.3, limit_down=9.7, band_mismatch=True))
    calm = _bar(close=10.05, pre_close=10.0)
    assert explain_block(engine, SYMBOL, 1, calm, None) == "unfillable"

    spiking = _bar(close=11.0, pre_close=10.0)
    assert explain_block(engine, SYMBOL, 1, spiking, None) == "limit_up"


def test_the_panel_halts_the_name_even_with_a_positive_volume() -> None:
    engine = _engine(tradability_index=_panel(is_suspended=True))
    assert explain_block(engine, SYMBOL, 1, _bar(volume=5_000.0), None) == "suspended"


def test_the_heuristic_still_halts_a_suspended_bar_without_a_panel() -> None:
    assert explain_block(_engine(), SYMBOL, 0, _bar(volume=0.0), None) == "suspended"


def test_a_missing_price_is_unfillable_rather_than_silent() -> None:
    assert explain_block(_engine(), SYMBOL, 1, _bar(close=None, open=0.0), None) == "unfillable"


# --------------------------------------------------------------------------
# engine construction
# --------------------------------------------------------------------------


def test_an_injected_panel_is_never_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _panel(limit_up=11.0)
    engine = make_engine({"tradability_index": panel})
    assert engine.tradability_index is panel


def test_the_lake_panel_is_attached_when_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = _panel(limit_up=11.0)
    monkeypatch.setattr(fills, "_tradability_panel", lambda: panel)
    assert make_engine({"tradability_index": None}).tradability_index is None
    assert make_engine().tradability_index is panel


def test_an_empty_panel_leaves_the_heuristic_in_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.data.tradability.load_panel", lambda: pd.DataFrame())
    _panel_switch(monkeypatch, True)
    assert fills._tradability_panel() is None


def test_the_panel_is_not_even_read_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off means off: the lake must not be opened for a feature that is disabled."""
    monkeypatch.setattr(
        "src.data.tradability.load_panel",
        lambda: pytest.fail("panel must not be loaded when the switch is off"),
    )
    _panel_switch(monkeypatch, False)
    assert fills._tradability_panel() is None


def test_a_loaded_panel_becomes_a_lookup_index(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = pd.DataFrame([{"symbol": SYMBOL, "date": DAY, "limit_up": 11.0}])
    monkeypatch.setattr("src.data.tradability.load_panel", lambda: frame)
    _panel_switch(monkeypatch, True)

    index = fills._tradability_panel()

    assert isinstance(index, TradabilityIndex)
    assert index.get(SYMBOL, DAY)["limit_up"] == 11.0


def test_a_broken_panel_does_not_break_fills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading the panel is best effort: a corrupt lake still fills orders."""
    monkeypatch.setattr("src.config.accessor.get_env_config", _raise_config)
    assert fills._tradability_panel() is None


def _panel_switch(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "src.config.accessor.get_env_config",
        lambda: type(
            "C", (), {"data": type("D", (), {"vibe_trading_tradability_panel": enabled})()}
        )(),
    )


def _raise_config():
    raise RuntimeError("config unavailable")


def _position(*, entry_time):
    from backtest.models import Position

    return Position(
        symbol=SYMBOL, direction=1, entry_price=10.0, entry_time=entry_time, size=500.0
    )
