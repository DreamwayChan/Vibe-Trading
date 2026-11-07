"""Tests for the tradability panel and its wiring into ChinaAEngine.

Covers the three things the engine could only guess before: halt status, the
price band (including the ST case the code prefix cannot reveal), and the
engine's decision to trust the panel over its own close-to-close heuristic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from src.data import tradability as td


# ---------------------------------------------------------------------------
# Price bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("000001.SZ", 0.10),   # Shenzhen main board
        ("600519.SH", 0.10),   # Shanghai main board
        ("300750.SZ", 0.20),   # ChiNext
        ("301029.SZ", 0.20),   # ChiNext post-2021 codes
        ("688981.SH", 0.20),   # STAR
        ("830799.BJ", 0.30),   # Beijing
        ("430047.BJ", 0.30),   # Beijing (migrated NEEQ code range)
    ],
)
def test_board_bands(symbol, expected):
    assert td.price_limit_pct(symbol) == expected


def test_st_band_overrides_board():
    """The blind spot the old helper documented: ST is 5% on every board."""
    assert td.price_limit_pct("600519.SH", is_st=True) == 0.05
    assert td.price_limit_pct("300750.SZ", is_st=True) == 0.05


def test_board_of():
    assert td.board_of("300750.SZ") == "chinext"
    assert td.board_of("688981") == "star"
    assert td.board_of("830799.BJ") == "bse"
    assert td.board_of("920060.BJ") == "bse"
    assert td.board_of("000001.SZ") == "main"


def test_limit_prices_round_half_up():
    up, down = td.limit_prices(10.01, "000001.SZ")
    assert (up, down) == (11.01, 9.01)


def test_limit_prices_st():
    up, down = td.limit_prices(10.0, "600519.SH", is_st=True)
    assert (up, down) == (10.5, 9.5)


def test_limit_prices_without_base():
    assert td.limit_prices(None, "000001.SZ") == (None, None)
    assert td.limit_prices(0, "000001.SZ") == (None, None)


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------


def _bars(dates, closes, volumes=None):
    volumes = volumes if volumes is not None else [1_000_000] * len(dates)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "close": closes,
            "volume": volumes,
            "amount": [c * v for c, v in zip(closes, volumes)],
        }
    )


def test_build_panel_marks_missing_session_as_halted():
    sessions = ["2024-03-04", "2024-03-05", "2024-03-06"]
    bars = {
        # 03-05 has no bar: the stock was halted that day.
        "000001.SZ": _bars(["2024-03-04", "2024-03-06"], [10.0, 10.5]),
    }
    panel = td.build_panel(bars, sessions=sessions)
    assert len(panel) == 3
    rows = panel.set_index("date")
    assert bool(rows.loc["2024-03-05", "is_suspended"]) is True
    assert bool(rows.loc["2024-03-04", "is_suspended"]) is False
    # First bar has no base, so no bands are invented for it.
    assert pd.isna(rows.loc["2024-03-04", "limit_up"])


def test_build_panel_zero_volume_is_halted():
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.0], [1e6, 0])}
    panel = td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"])
    assert bool(panel.iloc[1]["is_suspended"]) is True


def test_build_panel_computes_bands_from_previous_close():
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 11.0])}
    panel = td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"])
    row = panel[panel["date"] == "2024-03-05"].iloc[0]
    assert row["pre_close"] == 10.0
    assert row["limit_up"] == 11.0
    assert row["limit_down"] == 9.0


def test_build_panel_uses_universe_flags():
    bars = {"600519.SH": _bars(["2024-03-04", "2024-03-05"], [100.0, 105.0])}
    universe = pd.DataFrame({"symbol": ["600519.SH"], "is_st": [True], "board": ["main"]})
    panel = td.build_panel(bars, universe=universe, sessions=["2024-03-04", "2024-03-05"])
    row = panel.iloc[1]
    assert bool(row["is_st"]) is True
    assert row["limit_up"] == 105.0  # 5% band, not 10%


def test_build_panel_prefers_supplied_pre_close():
    frame = _bars(["2024-03-04", "2024-03-05"], [10.0, 11.0])
    frame["pre_close"] = [9.8, 10.0]
    panel = td.build_panel({"000001.SZ": frame}, sessions=["2024-03-04", "2024-03-05"])
    assert panel.iloc[1]["pre_close"] == 10.0


def test_build_panel_empty_input():
    assert td.build_panel({}).empty


def test_band_mismatch_flags_adjusted_series():
    """A 12.8% move on a 10% board can only mean adjusted prices."""
    bars = {"000001.SZ": _bars(["2024-02-20", "2024-02-21"], [7.648, 8.628])}
    panel = td.build_panel(bars, sessions=["2024-02-20", "2024-02-21"])
    assert bool(panel.iloc[1]["band_mismatch"]) is True


def test_band_mismatch_clear_on_normal_move():
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.5])}
    panel = td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"])
    assert bool(panel.iloc[1]["band_mismatch"]) is False


def test_band_mismatch_false_when_no_base():
    bars = {"000001.SZ": _bars(["2024-03-04"], [10.0])}
    panel = td.build_panel(bars, sessions=["2024-03-04"])
    assert bool(panel.iloc[0]["band_mismatch"]) is False


def test_build_panel_multiple_symbols_sorted():
    bars = {
        "600519.SH": _bars(["2024-03-04", "2024-03-05"], [1.0, 2.0]),
        "000001.SZ": _bars(["2024-03-04", "2024-03-05"], [3.0, 4.0]),
    }
    panel = td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"])
    assert len(panel) == 4
    assert list(panel["date"]) == sorted(panel["date"])


# ---------------------------------------------------------------------------
# Persistence and lookup
# ---------------------------------------------------------------------------


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    yield tmp_path


def test_panel_roundtrip_and_upsert(lake: Path):
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.5])}
    panel = td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"])
    assert td.save_panel(panel) == 2
    # Re-saving the same key must not duplicate rows.
    assert td.save_panel(panel) == 2

    loaded = td.load_panel()
    assert len(loaded) == 2
    assert set(loaded["symbol"]) == {"000001.SZ"}


def test_load_panel_filters(lake: Path):
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.5])}
    td.save_panel(td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"]))
    assert len(td.load_panel(start="2024-03-05")) == 1
    assert len(td.load_panel(symbols=["600519.SH"])) == 0


def test_load_panel_missing_returns_empty(lake: Path):
    assert td.load_panel().empty


def test_index_lookup_accepts_date_and_timestamp():
    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.5])}
    index = td.TradabilityIndex(td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"]))
    assert len(index) == 2
    assert index.get("000001.SZ", dt.date(2024, 3, 5))["close"] == 10.5
    assert index.get("000001.SZ", pd.Timestamp("2024-03-05"))["close"] == 10.5
    assert index.get("000001.SZ", dt.date(2024, 3, 6)) is None
    assert index.get("600519.SH", dt.date(2024, 3, 5)) is None


# ---------------------------------------------------------------------------
# Engine wiring
# ---------------------------------------------------------------------------


def _engine(panel: pd.DataFrame) -> ChinaAEngine:
    return ChinaAEngine({"tradability_index": td.TradabilityIndex(panel)})


def _bar(close: float, day: str = "2024-03-05", **extra) -> pd.Series:
    data = {"close": close, "open": close, "trade_date": pd.Timestamp(day), **extra}
    return pd.Series(data)


def test_engine_blocks_buy_on_halted_day():
    panel = pd.DataFrame(
        [
            {
                "symbol": "000001.SZ", "date": "2024-03-05", "is_suspended": True,
                "is_st": False, "board": "main", "pre_close": 10.0,
                "limit_up": 11.0, "limit_down": 9.0, "close": 10.0, "amount": 0.0,
            }
        ]
    )
    engine = _engine(panel)
    assert engine.can_execute("000001.SZ", 1, _bar(10.0)) is False
    assert engine.can_execute("000001.SZ", 0, _bar(10.0)) is False


def test_engine_blocks_buy_at_limit_up_price():
    panel = pd.DataFrame(
        [
            {
                "symbol": "000001.SZ", "date": "2024-03-05", "is_suspended": False,
                "is_st": False, "board": "main", "pre_close": 10.0,
                "limit_up": 11.0, "limit_down": 9.0, "close": 11.0, "amount": 1e8,
            }
        ]
    )
    engine = _engine(panel)
    assert engine.can_execute("000001.SZ", 1, _bar(11.0)) is False
    # Two cents below the band is tradable again.
    assert engine.can_execute("000001.SZ", 1, _bar(10.98)) is True


def test_engine_blocks_sell_at_limit_down_price():
    panel = pd.DataFrame(
        [
            {
                "symbol": "000001.SZ", "date": "2024-03-05", "is_suspended": False,
                "is_st": False, "board": "main", "pre_close": 10.0,
                "limit_up": 11.0, "limit_down": 9.0, "close": 9.0, "amount": 1e8,
            }
        ]
    )
    engine = _engine(panel)
    assert engine.can_execute("000001.SZ", 0, _bar(9.0)) is False
    assert engine.can_execute("000001.SZ", 0, _bar(9.02)) is True


def test_engine_uses_panel_band_not_close_move():
    """A 9.9% move is a limit-up on a 10% board but not on an ST 5% board.

    The heuristic would allow the buy (9.9% < 10%); the panel's 5% band must
    block it.
    """
    panel = pd.DataFrame(
        [
            {
                "symbol": "600519.SH", "date": "2024-03-05", "is_suspended": False,
                "is_st": True, "board": "main", "pre_close": 10.0,
                "limit_up": 10.5, "limit_down": 9.5, "close": 10.99, "amount": 1e8,
            }
        ]
    )
    engine = _engine(panel)
    assert engine.can_execute("600519.SH", 1, _bar(10.99)) is False


def test_engine_falls_back_when_day_missing_from_panel():
    panel = pd.DataFrame(
        [
            {
                "symbol": "000001.SZ", "date": "2024-03-05", "is_suspended": False,
                "is_st": False, "board": "main", "pre_close": 10.0,
                "limit_up": 11.0, "limit_down": 9.0, "close": 11.0, "amount": 1e8,
            }
        ]
    )
    engine = _engine(panel)
    # 2024-03-06 is unknown to the panel: the pct_chg heuristic decides again.
    assert engine.can_execute("000001.SZ", 1, _bar(11.0, day="2024-03-06", pct_chg=10.0)) is False
    assert engine.can_execute("000001.SZ", 1, _bar(10.1, day="2024-03-06", pct_chg=1.0)) is True


def test_engine_without_panel_keeps_old_behaviour():
    engine = ChinaAEngine({})
    assert engine.can_execute("300750.SZ", 1, _bar(12.0, pct_chg=20.0)) is False
    assert engine.can_execute("300750.SZ", 1, _bar(10.5, pct_chg=5.0)) is True


def test_engine_ignores_band_when_source_is_adjusted():
    """band_mismatch: keep the halt flag, hand the band back to the heuristic."""
    panel = pd.DataFrame(
        [
            {
                "symbol": "000001.SZ", "date": "2024-02-21", "is_suspended": False,
                "is_st": False, "board": "main", "pre_close": 7.648,
                "limit_up": 8.41, "limit_down": 6.88, "band_mismatch": True,
                "close": 8.628, "amount": 1e8,
            }
        ]
    )
    engine = _engine(panel)
    # 12.8% is above the heuristic's 10% band too, so the buy stays blocked.
    assert engine.can_execute("000001.SZ", 1, _bar(8.628, day="2024-02-21", pct_chg=12.8)) is False
    # But a normal +2% day is allowed even though close > adjusted limit_up.
    assert engine.can_execute("000001.SZ", 1, _bar(8.628, day="2024-02-21", pct_chg=2.0)) is True


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------


def test_runner_attaches_panel_by_default(lake: Path, monkeypatch: pytest.MonkeyPatch):
    from backtest.runner import _attach_tradability_panel
    from src.config.accessor import reset_env_config

    bars = {"000001.SZ": _bars(["2024-03-04", "2024-03-05"], [10.0, 10.5])}
    td.save_panel(td.build_panel(bars, sessions=["2024-03-04", "2024-03-05"]))
    config = {"start_date": "2024-03-01", "end_date": "2024-03-31"}

    reset_env_config()
    attached = _attach_tradability_panel(config, ["000001.SZ"])
    assert "tradability_index" in attached
    assert len(attached["tradability_index"]) == 2
    # The caller's config must not be mutated.
    assert "tradability_index" not in config

    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "0")
    reset_env_config()
    assert "tradability_index" not in _attach_tradability_panel(config, ["000001.SZ"])


def test_runner_skips_non_a_share(lake: Path, monkeypatch: pytest.MonkeyPatch):
    from backtest.runner import _attach_tradability_panel
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "1")
    reset_env_config()
    assert "tradability_index" not in _attach_tradability_panel({}, ["AAPL.US"])


def test_runner_without_panel_data_is_a_noop(lake: Path, monkeypatch: pytest.MonkeyPatch):
    from backtest.runner import _attach_tradability_panel
    from src.config.accessor import reset_env_config

    monkeypatch.setenv("VIBE_TRADING_TRADABILITY_PANEL", "1")
    reset_env_config()
    config = {"start_date": "2024-03-01", "end_date": "2024-03-31"}
    assert "tradability_index" not in _attach_tradability_panel(config, ["000001.SZ"])
