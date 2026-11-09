"""Risk markers that only fire when the tradability panel is involved.

``test_ashare_risk.py`` covers the close-to-close heuristic on a hand-built bar
list. The panel is the other half: when a built panel exists it is authoritative
— the exchange band, the ST designation and a halt can all disagree with what
the code prefix and yesterday's close imply. These tests pin which source wins,
and that a missing panel degrades to the heuristic instead of inventing flags.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.config.accessor import reset_env_config
from src.monitor import ashare_risk as risk
from src.monitor.models import MonitorSuggestion


def _bar(
    day: str,
    *,
    open_: float | None = 10.0,
    high: float | None = 10.2,
    low: float | None = 9.8,
    close: float | None = 10.0,
    pre_close: float | None = 10.0,
    volume: object = 1_000_000,
    **extra: object,
) -> dict:
    row = {
        "date": day,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": pre_close,
        "volume": volume,
        "is_st": False,
        "name": "",
    }
    row.update(extra)
    return row


def _frame(**columns: list) -> pd.DataFrame:
    index = pd.to_datetime(columns.pop("index", ["2024-06-03"]))
    return pd.DataFrame(columns, index=index)


@pytest.fixture(autouse=True)
def _reset_env() -> None:
    reset_env_config()
    yield  # type: ignore[misc]
    reset_env_config()


# ---------------------------------------------------------------------------
# Panel precedence
# ---------------------------------------------------------------------------


def test_panel_band_decides_when_the_code_band_would_not() -> None:
    """A 10.5 limit on a 10.00 close is a limit-up only the panel knows about."""
    rows = [_bar("2024-06-04", close=10.5, open_=10.2, high=10.5, low=10.2)]
    panel = {("600519.SH", "2024-06-04"): {"limit_up": 10.5, "band_mismatch": False}}

    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04", panel=panel)

    assert "limit_up" in flags
    assert "one_word" not in flags  # opened below the ceiling


def test_band_mismatch_falls_back_to_the_computed_band() -> None:
    """``band_mismatch`` marks a panel row whose band disagrees with the data."""
    rows = [_bar("2024-06-04", close=11.0, open_=11.0, high=11.0, low=11.0, pre_close=10.0)]
    panel = {
        ("600519.SH", "2024-06-04"): {
            "limit_up": 9.0,
            "band_mismatch": True,
        }
    }

    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04", panel=panel)

    assert "limit_up" in flags
    assert "one_word" in flags


def test_panel_suspension_cancels_the_limit_up_marker() -> None:
    """A halted name prints yesterday's close; that is not a sealed board."""
    rows = [_bar("2024-06-04", close=11.0, open_=11.0, high=11.0, low=11.0, pre_close=10.0)]
    panel = {("600519.SH", "2024-06-04"): {"is_suspended": True}}

    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04", panel=panel)

    assert flags == []


def test_panel_rows_are_also_found_by_bare_code() -> None:
    rows = [_bar("2024-06-04", close=10.1)]
    panel = {("600519", "2024-06-04"): {"is_st": "1"}}

    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04", panel=panel)

    assert flags == ["st"]


def test_panel_without_a_row_for_this_day_reads_as_no_panel() -> None:
    """A panel slice that covers other names must not mark this one."""
    rows = [_bar("2024-06-04", close=10.1)]
    panel = {("000002.SZ", "2024-06-04"): {"is_st": True}}

    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04", panel=panel)

    assert flags == []


def test_panel_slice_is_used_when_stamp_runs_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stamp_suggestions`` loads the panel itself when the caller has none."""
    seen: dict[str, object] = {}

    def fake_load_panel(*, start: str, end: str, symbols: list[str]) -> pd.DataFrame:
        seen.update(start=start, end=end, symbols=list(symbols))
        return pd.DataFrame(
            {
                "symbol": ["600519.SH"],
                "date": ["2024-06-04"],
                "is_st": [True],
                "limit_up": [11.0],
                "band_mismatch": [False],
            }
        )

    import src.data.tradability as tradability

    monkeypatch.setattr(tradability, "load_panel", fake_load_panel)
    suggestion = MonitorSuggestion(
        symbol="600519.SH", action="buy", reason="x", as_of="2024-06-04"
    )

    risk.stamp_suggestions(
        [suggestion],
        {
            "600519.SH": _frame(
                index=["2024-06-04"], close=[10.0], open=[10.0], high=[10.0], low=[10.0]
            )
        },
        as_of="2024-06-04",
    )

    assert suggestion.risk_flags == ["st"]
    assert seen["symbols"] == ["600519.SH"]
    assert seen["end"] == "2024-06-04"
    assert str(seen["start"]) < "2024-06-04"


def test_panel_slice_is_skipped_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "0")
    reset_env_config()
    import src.data.tradability as tradability

    def _must_not_run(**_kwargs: object) -> pd.DataFrame:
        raise AssertionError("panel must not be read when the switch is off")

    monkeypatch.setattr(tradability, "load_panel", _must_not_run)

    assert risk._load_panel(["600519.SH"], "2024-06-04") == {}


def test_panel_read_error_leaves_the_heuristic_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.data.tradability as tradability

    def _boom(**_kwargs: object) -> pd.DataFrame:
        raise OSError("lake is locked")

    monkeypatch.setattr(tradability, "load_panel", _boom)

    assert risk._load_panel(["600519.SH"], "2024-06-04") == {}


def test_panel_read_is_skipped_without_symbols() -> None:
    assert risk._load_panel([], "2024-06-04") == {}


def test_panel_slice_is_keyed_by_symbol_and_day(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.data.tradability as tradability

    monkeypatch.setattr(
        tradability,
        "load_panel",
        lambda **_kwargs: pd.DataFrame(
            {
                "symbol": ["600519.sh", "600519.SH"],
                "date": ["2024-06-04T00:00:00", "2024-06-05"],
                "is_st": [False, True],
            }
        ),
    )

    panel = risk._load_panel(["600519.SH"], "2024-06-05")

    assert panel[("600519.SH", "2024-06-04")]["is_st"] is False
    assert panel[("600519.SH", "2024-06-05")]["is_st"] is True


# ---------------------------------------------------------------------------
# Bar-side edges
# ---------------------------------------------------------------------------


def test_st_is_read_from_the_row_flag_not_only_the_name() -> None:
    rows = [_bar("2024-06-04", is_st=True)]
    assert risk.assess_flags("600001.SH", rows, as_of="2024-06-04") == ["st"]


@pytest.mark.parametrize("name", ["*ST示例", "ST示例", "S*ST示例", " st 示例 ", "st示例"])
def test_st_prefixes_are_recognised_with_noise(name: str) -> None:
    rows = [_bar("2024-06-04", name=name)]
    assert risk.assess_flags("600001.SH", rows, as_of="2024-06-04") == ["st"]


@pytest.mark.parametrize("name", ["", "   ", None, "中ST", "科技ST"])
def test_names_that_only_contain_st_are_not_st(name: object) -> None:
    rows = [_bar("2024-06-04", name=name)]
    assert risk.assess_flags("600001.SH", rows, as_of="2024-06-04") == []


@pytest.mark.parametrize("volume", [0, 0.0, -1])
def test_a_halted_bar_is_not_a_limit_up(volume: object) -> None:
    """Zero volume is a halt: the close is carried over, not a sealed board."""
    rows = [_bar("2024-06-04", close=11.0, open_=11.0, high=11.0, low=11.0, volume=volume)]
    assert risk.assess_flags("600519.SH", rows, as_of="2024-06-04") == []


@pytest.mark.parametrize("volume", [None, "n/a", ""])
def test_a_bar_without_volume_is_not_silently_unblocked(volume: object) -> None:
    """No volume column is not the same as zero volume.

    Treating an unreadable volume as a halt would drop the limit-up marker from
    a real sealed board, so the marker stays — only a readable ``<= 0`` halts.
    """
    rows = [_bar("2024-06-04", close=11.0, open_=11.0, high=11.0, low=11.0, volume=volume)]
    assert "limit_up" in risk.assess_flags("600519.SH", rows, as_of="2024-06-04")


def test_missing_ohlc_does_not_invent_a_one_word_board() -> None:
    """A bar with no open/high/low is a limit-up, not a sealed board."""
    rows = [_bar("2024-06-04", close=11.0, open_=None, high=11.0, low=11.0)]
    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04")
    assert flags == ["limit_up"]


def test_missing_pre_close_cannot_be_a_limit_up() -> None:
    rows = [_bar("2024-06-04", close=11.0, pre_close=None)]
    assert risk.assess_flags("600519.SH", rows, as_of="2024-06-04") == []


def test_a_two_day_streak_is_recorded_but_not_extreme() -> None:
    rows = [
        _bar("2024-06-03", close=10.0, open_=10.0, high=10.0, low=10.0, pre_close=9.09),
        _bar("2024-06-04", close=11.0, open_=11.0, high=11.0, low=11.0, pre_close=10.0),
    ]
    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-04")
    assert "consecutive_board" in flags
    assert "extreme_board" not in flags


def test_a_streak_broken_by_one_quiet_day_resets() -> None:
    rows = [
        _bar("2024-06-03", close=11.0, open_=11.0, high=11.0, low=11.0, pre_close=10.0),
        _bar("2024-06-04", close=11.0, open_=10.9, high=11.0, low=10.8, pre_close=11.0),
        _bar("2024-06-05", close=12.1, open_=12.1, high=12.1, low=12.1, pre_close=11.0),
    ]
    flags = risk.assess_flags("600519.SH", rows, as_of="2024-06-05")
    assert "consecutive_board" not in flags


def test_rows_after_as_of_are_ignored() -> None:
    rows = [_bar("2024-06-05", close=11.0, open_=11.0, high=11.0, low=11.0)]
    assert risk.assess_flags("600519.SH", rows, as_of="2024-06-04") == []


def test_unknown_symbol_stays_unmarked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suggestion = MonitorSuggestion(symbol="000002.SZ", action="buy", reason="x", as_of="2024-06-04")
    risk.stamp_suggestions(
        [suggestion], {"600519.SH": _frame(close=[11.0])}, as_of="2024-06-04", panel={}
    )
    assert suggestion.risk_flags == []


def test_empty_inputs_come_back_unchanged() -> None:
    suggestion = MonitorSuggestion(symbol="600519.SH", action="buy", reason="x", as_of="2024-06-04")

    assert risk.stamp_suggestions([], {"600519.SH": _frame(close=[11.0])}, as_of="2024-06-04") == []
    assert risk.stamp_suggestions([suggestion], {}, as_of="2024-06-04") == [suggestion]
    assert suggestion.risk_flags == []


# ---------------------------------------------------------------------------
# Frame normalisation
# ---------------------------------------------------------------------------


def test_frame_rows_fill_pre_close_from_the_previous_bar() -> None:
    frame = _frame(
        index=["2024-06-03", "2024-06-04"],
        close=[10.0, 11.0],
        open=[9.9, 11.0],
        high=[10.1, 11.0],
        low=[9.8, 11.0],
        volume=[1_000_000, 1_000_000],
    )

    rows = risk._frame_rows(frame)

    assert rows[0]["pre_close"] is None  # nothing before the first bar
    assert rows[1]["pre_close"] == 10.0
    assert [row["date"] for row in rows] == ["2024-06-03", "2024-06-04"]


def test_frame_rows_prefer_an_explicit_pre_close_column() -> None:
    frame = _frame(close=[11.0], pre_close=[10.0])
    assert risk._frame_rows(frame)[0]["pre_close"] == 10.0


def test_frame_rows_skip_rows_with_no_usable_date() -> None:
    frame = pd.DataFrame({"close": [10.0, 11.0]}, index=[pd.NaT, pd.Timestamp("2024-06-04")])
    rows = risk._frame_rows(frame)
    assert [row["date"] for row in rows] == ["2024-06-04"]


def test_empty_frame_has_no_rows() -> None:
    assert risk._frame_rows(pd.DataFrame()) == []


def test_frame_rows_drop_non_positive_closes_and_keep_the_trading_date_column() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": ["2024-06-03", "2024-06-04"],
            "close": [0.0, 11.0],
            "volume": [0, 1_000_000],
        },
        index=[0, 1],
    )

    rows = risk._frame_rows(frame)

    assert [row["date"] for row in rows] == ["2024-06-03", "2024-06-04"]
    assert rows[0]["close"] is None


def test_missing_cells_read_as_none() -> None:
    frame = pd.DataFrame({"close": [11.0]}, index=pd.to_datetime(["2024-06-04"]))
    row = risk._frame_rows(frame)[0]
    assert row["open"] is None
    assert row["is_st"] is None
    assert row["name"] is None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False), ("yes", True), ("Y", True),
     ("true", True), ("1", True), ("0", False), ("no", False), (None, False)],
)
def test_as_bool(value: object, expected: bool) -> None:
    assert risk._as_bool(value) is expected


def test_as_bool_tolerates_values_pandas_cannot_reduce() -> None:
    assert risk._as_bool(["x"]) is True
    assert risk._as_bool([]) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(10.0, 10.0), ("10.5", 10.5), (0, None), (-1, None), (None, None), ("abc", None),
     (float("nan"), None)],
)
def test_positive(value: object, expected: float | None) -> None:
    assert risk._positive(value) == expected


def test_date_text_handles_everything_the_panel_can_return() -> None:
    assert risk._date_text("2024-06-04") == "2024-06-04"
    assert risk._date_text(pd.Timestamp("2024-06-04 15:00")) == "2024-06-04"
    assert risk._date_text(None) is None
    assert risk._date_text(pd.NaT) is None
    assert risk._date_text("NaT") is None
    assert risk._date_text("not-a-date") is None
    # A multi-valued cell makes ``pd.isna`` ambiguous; it must not escape.
    assert risk._date_text(["2024-06-04", "2024-06-05"]) is None


def test_cell_returns_none_for_missing_or_na_cells() -> None:
    series = pd.Series({"a": 1.0, "b": float("nan"), "c": ["x", "y"]})
    assert risk._cell(series, "a") == 1.0
    assert risk._cell(series, "missing") is None
    assert risk._cell(series, "b") is None
    # ``pd.isna`` cannot reduce a multi-valued cell, so the raw value is kept.
    assert risk._cell(series, "c") == ["x", "y"]


def test_norm_is_case_and_space_insensitive() -> None:
    assert risk._norm(" 600519.sh ") == "600519.SH"
    assert risk._norm(None) == ""


def test_buy_block_reasons_dedupe_and_ignore_advisory_flags() -> None:
    assert risk.buy_block_reasons(["limit_up", "limit_up", "st", "consecutive_board"]) == [
        "ashare_limit_up",
        "ashare_st",
    ]
    assert risk.buy_block_reasons(None) == []
    assert risk.buy_block_reasons([]) == []


# ---------------------------------------------------------------------------
# The shared fx_small panel
# ---------------------------------------------------------------------------


def test_fx_small_marks_every_hazard(ashare_frames, ashare_as_of: str) -> None:
    """The shared panel must produce exactly the markers it advertises."""
    import ashare_fixtures

    for symbol, expected in ashare_fixtures.EXPECTED_FLAGS.items():
        flags = risk.assess_flags(
            symbol, risk._frame_rows(ashare_frames[symbol]), as_of=ashare_as_of
        )
        assert flags == expected, symbol


def test_fx_small_only_blocks_the_buys_that_must_be_blocked(
    ashare_frames, ashare_as_of: str
) -> None:
    """Four of the five names carry a buy block; the clean one must survive."""
    suggestions = [
        MonitorSuggestion(symbol=symbol, action="buy", reason="scan", as_of=ashare_as_of)
        for symbol in ashare_frames
    ]

    risk.stamp_suggestions(suggestions, ashare_frames, as_of=ashare_as_of, panel={})

    blocked = {s.symbol: risk.buy_block_reasons(s.risk_flags) for s in suggestions}
    assert blocked["600519.SH"] == []
    assert blocked["600001.SH"] == ["ashare_st"]
    assert blocked["600002.SH"] == []  # a halt is a fill problem, not a sentiment one
    assert blocked["600003.SH"] == ["ashare_limit_up", "ashare_one_word"]
    assert blocked["600004.SH"] == ["ashare_limit_up", "ashare_extreme_board"]
