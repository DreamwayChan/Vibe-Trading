"""Unit tests for monitor analog forecast (shape match → up/down 10-day paths)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.monitor.forecast import (
    FORECAST_LOOKBACK_DAYS,
    HORIZON,
    MAX_NEIGHBORS,
    MARKET_INDEX,
    MARKET_VETO_DISAGREE,
    PATTERN_WINDOW,
    active_holdings,
    find_analogs,
    forecast_symbol,
    directional_bias,
    apply_market_veto,
    limit_up_pct,
    next_weekdays,
    run_monitor_forecast,
    search_analogs,
    _trend_rule,
)
from src.monitor.forecast import _softmax_weights
from src.monitor.models import HoldingRow, MonitorConfig, StrategyRef
from src.monitor.scan import MonitorScanError


def _ohlcv_from_closes(closes: list[float], start: str = "2023-01-02") -> pd.DataFrame:
    """Build a tradable OHLC frame around a close path (small wicks, no gaps)."""
    idx = pd.bdate_range(start=start, periods=len(closes))
    rows = []
    prev = closes[0]
    for close in closes:
        open_px = prev
        high = max(open_px, close) * 1.004
        low = min(open_px, close) * 0.996
        rows.append({"open": open_px, "high": high, "low": low, "close": close, "volume": 1_000.0})
        prev = close
    return pd.DataFrame(rows, index=idx)


def test_limit_up_pct_board_vs_chinext() -> None:
    assert limit_up_pct("600519.SH") == pytest.approx(0.10)
    assert limit_up_pct("000858.SZ") == pytest.approx(0.10)
    assert limit_up_pct("300750.SZ") == pytest.approx(0.20)
    assert limit_up_pct("688981.SH") == pytest.approx(0.20)
    assert limit_up_pct("830799.BJ") == pytest.approx(0.20)


def test_next_weekdays_skips_weekend() -> None:
    # Friday 2024-01-05 → next 10 sessions start Monday 2024-01-08.
    days = next_weekdays(date(2024, 1, 5), 10)
    assert days[0] == "2024-01-08"
    assert len(days) == 10
    assert all(date.fromisoformat(d).weekday() < 5 for d in days)


def test_active_holdings_skips_exit_and_zero_weight() -> None:
    rows = [
        HoldingRow(symbol="AAA.SZ", weight=0.2, action="hold"),
        HoldingRow(symbol="BBB.SZ", weight=0.1, action="exit"),
        HoldingRow(symbol="CCC.SZ", weight=0.0, action="hold"),
        HoldingRow(symbol="DDD.SZ", weight=0.3, action="enter"),
    ]
    picked = active_holdings(rows)
    assert [r.symbol for r in picked] == ["DDD.SZ", "AAA.SZ"]


def test_find_analogs_prefers_matching_window() -> None:
    """A unique 20-day grind that later rallied should outrank noise windows."""
    rng = np.random.default_rng(0)
    noise = rng.normal(0.0, 0.012, size=160).tolist()
    # Non-constant: a flat window z-scores to zeros and cannot match on shape.
    pattern = ([0.004, 0.010, 0.006, 0.012, 0.008] * 4)
    rally = [0.02] * HORIZON
    # Query equals `pattern`; one historical copy is followed by `rally`.
    returns = np.array(noise + pattern + rally + [0.0] * 30 + pattern, dtype=float)
    matches = find_analogs(returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=5)
    assert matches
    assert matches[0].shape_sim > 0.99
    assert matches[0].similarity > 0.90
    assert matches[0].forward_return > 0


def test_forecast_symbol_known_up_analogs_bias_up() -> None:
    """Repeat a 20-day up-grind followed by a 10-day rally, then query the grind."""
    pattern = [1.004, 1.010, 1.006, 1.012, 1.008] * 4
    rally = [1.02, 1.03, 1.018, 1.028, 1.022] * 2
    fade = [0.99, 0.98, 0.992, 0.978, 0.985] * 2
    closes = [100.0]

    def _apply(mults: list[float]) -> None:
        for m in mults:
            closes.append(closes[-1] * m)

    _apply([1.0] * 5)
    for _ in range(4):
        _apply(pattern)
        _apply(rally)
    _apply(pattern)
    _apply(fade)
    _apply([1.001] * 25)
    _apply(pattern)

    frame = _ohlcv_from_closes(closes)
    out = forecast_symbol("600519.SH", frame, weight=0.2)
    assert out["error"] is None
    assert len(out["up_bars"]) == HORIZON
    assert len(out["down_bars"]) == HORIZON
    assert len(out["history"]) >= PATTERN_WINDOW
    assert out["up_bars"][-1]["close"] > out["price"]
    assert out["down_bars"][-1]["close"] < out["price"]
    assert out["assessment"]["analog_count"] > 0
    assert out["assessment"]["up_count"] >= out["assessment"]["down_count"]
    assert out["up_prob"] >= 0.5
    assert out["assessment"]["called"] is True
    assert out["assessment"]["bias"] == "up"
    assert out["ma"]["up"]["ma5"][-1] is not None
    assert len(out["ma"]["history"]["ma20"]) == len(out["history"])
    assert "confidence" in out["assessment"]
    assert 0.0 <= out["assessment"]["confidence"] <= 1.0


def test_forecast_symbol_insufficient_history() -> None:
    frame = _ohlcv_from_closes([100.0 + i * 0.1 for i in range(10)])
    out = forecast_symbol("000858.SZ", frame)
    assert out["error"]
    assert out["up_bars"] == []


def test_run_monitor_forecast_latest_board_without_snapshot(tmp_path: Path) -> None:
    closes = [50.0]
    for i in range(90):
        closes.append(closes[-1] * (1.003 if i % 7 else 0.997))
    frame = _ohlcv_from_closes(closes)
    config = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_forecast"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.4, action="hold")],
    )

    def fake_fetch(symbols, start, end, source, interval):
        return {s: frame.copy() for s in symbols}, {}

    report = run_monitor_forecast(
        config,
        as_of=None,
        fetch_ohlcv=fake_fetch,
        apply_live_overlay=False,
        runs_root=tmp_path,
    )
    assert report["evaluated_count"] == 1
    assert report["horizon"] == HORIZON
    assert "called_count" in report
    item = report["items"][0]
    assert item["symbol"] == "AAA.SZ"
    assert len(item["up_bars"]) == HORIZON
    assert len(item["down_bars"]) == HORIZON
    last_hist = date.fromisoformat(item["history"][-1]["time"])
    first_future = date.fromisoformat(item["up_bars"][0]["time"])
    assert first_future > last_hist
    assert first_future.weekday() < 5


def test_run_monitor_forecast_missing_snapshot_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_missing_snap"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.2, action="hold")],
    )
    with pytest.raises(MonitorScanError, match="no snapshot"):
        run_monitor_forecast(
            config,
            as_of="2024-01-02",
            fetch_ohlcv=lambda *_a: ({}, {}),
            apply_live_overlay=False,
            runs_root=tmp_path,
        )


def test_forecast_snapshot_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.monitor.store import list_forecast_summaries, load_forecast_snapshot, save_forecast_snapshot

    report = {
        "as_of": "2024-01-02",
        "horizon": 10,
        "holding_count": 1,
        "evaluated_count": 1,
        "capped": False,
        "bias": "up",
        "items": [{"symbol": "AAA.SZ", "weight": 0.2, "up_prob": 0.6, "down_prob": 0.4}],
    }
    first = save_forecast_snapshot("run_save_me", report)
    second = save_forecast_snapshot("run_save_me", {**report, "bias": "down"})
    assert first["id"] != second["id"] or first["saved_at"] != second["saved_at"]
    loaded = load_forecast_snapshot("run_save_me", first["id"])
    assert loaded is not None
    assert loaded["items"][0]["symbol"] == "AAA.SZ"
    summaries = list_forecast_summaries("run_save_me", limit=10)
    assert len(summaries) == 2
    assert "items" not in summaries[0]
    assert summaries[0]["id"] == second["id"]


def test_find_analogs_dedupes_overlapping_windows() -> None:
    """A slow-trend episode must not flood Top-K with 1-day-shifted copies."""
    rng = np.random.default_rng(2)
    noise = rng.normal(0.0, 0.02, size=90)
    # Affine 20-day slices z-score to the same ramp, so overlapping windows all look alike.
    episode = np.linspace(0.004, 0.014, 80)
    query = episode[-PATTERN_WINDOW:]
    returns = np.concatenate([noise, episode, rng.normal(0.0, 0.02, size=25), query])

    packed = find_analogs(
        returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=MAX_NEIGHBORS, dedupe=False
    )
    spaced = find_analogs(
        returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=MAX_NEIGHBORS, dedupe=True
    )
    assert packed and spaced

    packed_ends = sorted(m.end_index for m in packed)
    packed_close = sum(
        1 for i in range(len(packed_ends) - 1) if packed_ends[i + 1] - packed_ends[i] < PATTERN_WINDOW / 2.0
    )
    assert packed_close >= 8

    high_spaced = [m for m in spaced if m.similarity > 0.85]
    assert high_spaced
    assert len(high_spaced) < len(packed)
    ends = sorted(m.end_index for m in high_spaced)
    min_gap = PATTERN_WINDOW / 2.0
    assert all(ends[i + 1] - ends[i] >= min_gap for i in range(len(ends) - 1))


def test_find_analogs_zscore_matches_same_shape_at_higher_vol() -> None:
    """Raw-return cosine would prefer similar vol; z-score should still find the loud copy."""
    rng = np.random.default_rng(3)
    pattern = rng.normal(0.0, 0.01, size=PATTERN_WINDOW)
    loud = pattern * 5.0
    noise = rng.normal(0.0, 0.02, size=80)
    rally = np.full(HORIZON, 0.015)
    filler = rng.normal(0.0, 0.02, size=40)
    returns = np.concatenate([noise, loud, rally, filler, pattern])

    matches = find_analogs(returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=5)
    assert matches
    loud_end = len(noise) + PATTERN_WINDOW
    assert matches[0].end_index == loud_end
    assert matches[0].shape_sim > 0.99
    assert matches[0].similarity > 0.75


def test_softmax_nearer_neighbor_dominates_vote() -> None:
    """tau=0.12 makes sim 0.95 swamp sim 0.55, so the closer analog owns the probability."""
    close_w, far_w = _softmax_weights(np.array([0.95, 0.55], dtype=float))
    assert close_w > 0.85
    assert close_w > far_w * 10

    # Perfect copy rallies; a weaker copy dumps. Weighted P(up) must stay well above 0.5.
    rng = np.random.default_rng(4)
    pattern = rng.normal(0.0, 0.01, size=PATTERN_WINDOW)
    weaker = pattern * 0.45 + rng.normal(0.0, 0.008, size=PATTERN_WINDOW)
    noise = rng.normal(0.0, 0.025, size=70)
    rally = np.full(HORIZON, 0.02)
    dump = np.full(HORIZON, -0.02)
    filler = rng.normal(0.0, 0.025, size=35)
    returns = np.concatenate([noise, weaker, dump, filler, pattern, rally, filler, pattern])

    matches = find_analogs(returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=8)
    up_w = sum(m.weight for m in matches if m.forward_return > 0)
    down_w = sum(m.weight for m in matches if m.forward_return < 0)
    total = up_w + down_w
    assert total > 0
    assert up_w / total > 0.6


def test_forecast_symbol_unlike_history_has_low_style_score() -> None:
    """A query never seen in the sample pool should not look like a high-style match."""
    rng = np.random.default_rng(5)
    closes = [100.0]
    for ret in rng.normal(0.0, 0.006, size=140):
        closes.append(closes[-1] * (1.0 + float(ret)))
    # Unique 20-day blast at the end — not a scaled copy of earlier noise.
    spike = [1.04, 0.97, 1.05, 0.96, 1.045, 0.965, 1.05, 0.96, 1.04, 0.97] * 2
    for m in spike:
        closes.append(closes[-1] * m)

    out = forecast_symbol("600519.SH", _ohlcv_from_closes(closes))
    assert out["error"] is None
    assert out["style_score"] < 0.40
    assert out["assessment"]["style_bucket"] == "low"
    assert out["assessment"]["called"] is False
    assert out["assessment"]["bias"] == "neutral"


def test_run_monitor_forecast_uses_800_day_lookback(tmp_path: Path) -> None:
    """Forecast fetch is independent of the 400-day monitor scan window."""
    closes = [50.0]
    for i in range(90):
        closes.append(closes[-1] * (1.003 if i % 7 else 0.997))
    frame = _ohlcv_from_closes(closes)
    config = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_forecast_lb"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.4, action="hold")],
    )
    captured: list[tuple[str, str, list[str]]] = []

    def fake_fetch(symbols, start, end, source, interval):
        captured.append((start, end, list(symbols)))
        return {s: frame.copy() for s in symbols}, {}

    report = run_monitor_forecast(
        config,
        as_of=None,
        fetch_ohlcv=fake_fetch,
        apply_live_overlay=False,
        runs_root=tmp_path,
    )
    assert captured
    start, end, fetched = captured[0]
    span = date.fromisoformat(end) - date.fromisoformat(start)
    assert span.days >= FORECAST_LOOKBACK_DAYS
    assert FORECAST_LOOKBACK_DAYS == 800
    assert "called_count" in report
    assert MARKET_INDEX in fetched
    assert report["market"]["symbol"] == MARKET_INDEX
    assert "decisive" in report["market"]
    assert "impact" in report["market"]


def test_run_monitor_forecast_live_fetch_ends_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live overlay should pull Tushare through T-1 so index_daily can hit disk cache."""
    closes = [50.0]
    for i in range(90):
        closes.append(closes[-1] * (1.003 if i % 7 else 0.997))
    frame = _ohlcv_from_closes(closes)
    config = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_forecast_t1"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.4, action="hold")],
    )
    captured: list[tuple[str, str]] = []

    def fake_fetch(symbols, start, end, source, interval):
        captured.append((start, end))
        return {s: frame.copy() for s in symbols}, {}

    import src.monitor.forecast as forecast_mod

    monkeypatch.setattr(
        forecast_mod,
        "_apply_same_day_overlay",
        lambda data_map, as_of_date=None: (data_map, {}, {}),
    )

    run_monitor_forecast(
        config,
        as_of=None,
        fetch_ohlcv=fake_fetch,
        apply_live_overlay=True,
        runs_root=tmp_path,
    )
    assert captured
    _start, end = captured[0]
    assert end == (date.today() - timedelta(days=1)).isoformat()


def test_directional_bias_abstains_unless_lopsided_and_confident() -> None:
    assert directional_bias(0.56, 0.08) == ("neutral", False)
    assert directional_bias(0.72, 0.10) == ("neutral", False)
    assert directional_bias(0.56, 0.40) == ("neutral", False)
    assert directional_bias(0.70, 0.20) == ("up", True)
    assert directional_bias(0.30, 0.20) == ("down", True)
    assert directional_bias(0.80, 0.30, style_score=0.20) == ("neutral", False)


def test_find_analogs_does_not_fill_to_max_k_with_weak_windows() -> None:
    """A handful of true copies must not be padded out to 24 noise neighbors."""
    rng = np.random.default_rng(7)
    pattern = [0.004, 0.010, 0.006, 0.012, 0.008] * 4
    rally = [0.018] * HORIZON
    noise = rng.normal(0.0, 0.02, size=80).tolist()
    filler = rng.normal(0.0, 0.02, size=25).tolist()
    body: list[float] = []
    for _ in range(4):
        body.extend(pattern)
        body.extend(rally)
        body.extend(filler)
    returns = np.array(noise + body + pattern, dtype=float)
    matches = find_analogs(
        returns, window=PATTERN_WINDOW, horizon=HORIZON, top_k=MAX_NEIGHBORS, min_k=6
    )
    assert matches
    assert len(matches) < MAX_NEIGHBORS
    best = matches[0].similarity
    assert all(m.similarity >= min(0.45, best - 0.20) for m in matches)


def test_market_is_decisive_requires_skew_confidence_and_impact() -> None:
    from src.monitor.forecast import market_is_decisive

    # Lopsided but a 1% analog grind is not a large-impact barometer.
    assert market_is_decisive(0.80, 0.40, 0.60, 0.01) == ("neutral", False)
    # 51% vote is two-sided even with high style.
    assert market_is_decisive(0.51, 0.40, 0.60, 0.05) == ("neutral", False)
    # Weak confidence / style stay idle.
    assert market_is_decisive(0.80, 0.10, 0.60, 0.05) == ("neutral", False)
    assert market_is_decisive(0.80, 0.40, 0.30, 0.05) == ("neutral", False)
    assert market_is_decisive(0.80, 0.40, 0.60, 0.04) == ("up", True)
    assert market_is_decisive(0.20, 0.40, 0.60, -0.05) == ("down", True)


def test_apply_market_veto_only_on_strong_disagreement() -> None:
    stock_up = {"bias": "up", "called": True, "market_veto": None}

    # Weak / idle index must not wipe a stock call.
    idle = apply_market_veto(stock_up, {"decisive": False, "called": False, "bias": "neutral"})
    assert idle["called"] is True
    assert idle["market_veto"] is None

    weak_called = apply_market_veto(
        stock_up, {"decisive": False, "called": True, "bias": "down"}
    )
    assert weak_called["called"] is True
    assert weak_called["market_veto"] is None

    disagree = apply_market_veto(
        stock_up, {"decisive": True, "called": True, "bias": "down"}
    )
    assert disagree["called"] is False
    assert disagree["bias"] == "neutral"
    assert disagree["market_veto"] == MARKET_VETO_DISAGREE

    agree = apply_market_veto(
        stock_up, {"decisive": True, "called": True, "bias": "up"}
    )
    assert agree["called"] is True
    assert agree["bias"] == "up"
    assert agree["market_veto"] is None

    # Missing index data must not wipe a stock call.
    degraded = apply_market_veto(stock_up, {"error": "no OHLCV", "called": False, "decisive": False})
    assert degraded["called"] is True


def test_run_monitor_forecast_keeps_stock_call_when_index_is_not_decisive(
    tmp_path: Path,
) -> None:
    """A weak CSI 300 analog must not silence a stock that already issued a call."""
    pattern = [1.004, 1.010, 1.006, 1.012, 1.008] * 4
    rally = [1.02, 1.03, 1.018, 1.028, 1.022] * 2
    fade = [0.99, 0.98, 0.992, 0.978, 0.985] * 2

    def _closes(follow: list[float]) -> list[float]:
        path = [100.0]
        for _ in range(5):
            path.append(path[-1])
        for _ in range(4):
            for m in pattern:
                path.append(path[-1] * m)
            for m in follow:
                path.append(path[-1] * m)
        for m in pattern:
            path.append(path[-1] * m)
        for m in fade:
            path.append(path[-1] * m)
        for _ in range(25):
            path.append(path[-1] * 1.001)
        for m in pattern:
            path.append(path[-1] * m)
        return path

    stock = _ohlcv_from_closes(_closes(rally))
    index = _ohlcv_from_closes(_closes(fade))
    config = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_veto_idle"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.4, action="hold")],
    )

    def fake_fetch(symbols, start, end, source, interval):
        out = {}
        for s in symbols:
            out[s] = index.copy() if s == MARKET_INDEX else stock.copy()
        return out, {}

    stock_solo = forecast_symbol("AAA.SZ", stock, weight=0.4)
    assert stock_solo["assessment"]["called"] is True
    assert stock_solo["assessment"]["bias"] == "up"

    report = run_monitor_forecast(
        config,
        as_of=None,
        fetch_ohlcv=fake_fetch,
        apply_live_overlay=False,
        runs_root=tmp_path,
    )
    item = report["items"][0]
    assert item["up_prob"] >= 0.5
    if not report["market"].get("decisive"):
        assert item["assessment"]["called"] is True
        assert item["assessment"]["market_veto"] is None
    else:
        # If the synthetic index happens to clear the strong bar, only reverse vetoes.
        if report["market"].get("bias") == "down":
            assert item["assessment"]["called"] is False
            assert item["assessment"]["market_veto"] == MARKET_VETO_DISAGREE
        else:
            assert item["assessment"]["called"] is True


def test_run_monitor_forecast_vetoes_stock_up_when_index_is_decisive_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strong opposite CSI 300 analog withholds the stock directional label."""
    pattern = [1.004, 1.010, 1.006, 1.012, 1.008] * 4
    rally = [1.02, 1.03, 1.018, 1.028, 1.022] * 2

    def _closes(follow: list[float]) -> list[float]:
        path = [100.0]
        for _ in range(5):
            path.append(path[-1])
        for _ in range(4):
            for m in pattern:
                path.append(path[-1] * m)
            for m in follow:
                path.append(path[-1] * m)
        for m in pattern:
            path.append(path[-1] * m)
        for _ in range(25):
            path.append(path[-1] * 1.001)
        for m in pattern:
            path.append(path[-1] * m)
        return path

    stock = _ohlcv_from_closes(_closes(rally))
    config = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_veto_down"),
        holdings=[HoldingRow(symbol="AAA.SZ", weight=0.4, action="hold")],
    )

    def fake_fetch(symbols, start, end, source, interval):
        return {s: stock.copy() for s in symbols}, {}

    import src.monitor.forecast as forecast_mod

    original_snapshot = forecast_mod._market_snapshot

    def force_down_barometer(item):
        snap = original_snapshot(item)
        snap["decisive"] = True
        snap["called"] = True
        snap["bias"] = "down"
        snap["impact"] = -0.05
        return snap

    monkeypatch.setattr(forecast_mod, "_market_snapshot", force_down_barometer)

    report = run_monitor_forecast(
        config,
        as_of=None,
        fetch_ohlcv=fake_fetch,
        apply_live_overlay=False,
        runs_root=tmp_path,
    )
    item = report["items"][0]
    assert item["assessment"]["called"] is False
    assert item["assessment"]["market_veto"] == MARKET_VETO_DISAGREE
    assert report["market"]["decisive"] is True
    assert report["market"]["bias"] == "down"


def _closes_from_returns(returns: np.ndarray, start: float = 100.0) -> np.ndarray:
    close = np.empty(len(returns) + 1, dtype=float)
    close[0] = start
    close[1:] = start * np.cumprod(1.0 + returns)
    return close


def test_activity_series_prefers_turnover_over_volume() -> None:
    from src.monitor.forecast import _activity_series

    idx = pd.bdate_range("2023-01-02", periods=40)
    volume = np.linspace(100.0, 400.0, 40)
    turnover = np.concatenate([np.full(20, 1.0), np.linspace(2.0, 8.0, 20)])
    frame = pd.DataFrame({"volume": volume, "turnover": turnover}, index=idx)
    got = _activity_series(frame)
    assert got is not None
    np.testing.assert_allclose(got, turnover)


def test_search_analogs_volume_shape_breaks_price_ties() -> None:
    """Same return shape: the copy whose volume burst matches today ranks first."""
    pattern = np.array([0.004, 0.010, 0.006, 0.012, 0.008] * 4, dtype=float)
    rally = np.full(HORIZON, 0.012)
    pad = np.zeros(30, dtype=float)
    returns = np.concatenate([pad, pattern, rally, pad, pattern, rally, pad, pattern])
    close = _closes_from_returns(returns)

    burst = np.array([1_000.0] * 15 + [8_000.0, 9_000.0, 10_000.0, 9_500.0, 8_500.0])
    activity = np.full(len(close), 1_000.0)
    # Return-space end of the first pattern copy; volume slice is close-aligned.
    end_burst = len(pad) + PATTERN_WINDOW
    activity[end_burst - PATTERN_WINDOW + 1 : end_burst + 1] = burst
    activity[-PATTERN_WINDOW:] = burst

    result = search_analogs(
        returns,
        close=close,
        activity=activity,
        window=PATTERN_WINDOW,
        horizon=HORIZON,
        max_k=5,
        min_k=1,
    )
    assert result.neighbors
    assert result.neighbors[0].end_index == end_burst
    assert result.neighbors[0].volume_shape_sim > 0.90
    end_flat = len(pad) + PATTERN_WINDOW + HORIZON + len(pad) + PATTERN_WINDOW
    assert result.neighbors[0].end_index != end_flat


def test_regime_compatible_allows_flat_or_same_sign() -> None:
    from src.monitor.forecast import _regime_compatible

    assert _regime_compatible((0.05, 0.01), (0.04, 0.012)) is True
    assert _regime_compatible((0.05, 0.01), (-0.04, 0.01)) is False
    # |20d return| < 1% is treated as flat, so sign is not a veto.
    assert _regime_compatible((0.005, 0.01), (-0.04, 0.01)) is True
    assert _regime_compatible((0.05, 0.01), (0.04, 0.05)) is False


def test_regime_ok_mask_drops_opposite_index_windows() -> None:
    from src.monitor.forecast import _regime_ok_mask

    rng = np.random.default_rng(21)
    n_stock = 120
    stock_idx = pd.bdate_range("2023-01-02", periods=n_stock)
    idx_closes = [100.0]
    for i in range(n_stock - 1):
        drift = 0.008 if i < 50 else -0.008
        idx_closes.append(idx_closes[-1] * (1.0 + drift + float(rng.normal(0.0, 0.003))))
    index_frame = pd.DataFrame({"close": idx_closes}, index=stock_idx)
    returns_len = n_stock - 1
    mask = _regime_ok_mask(
        stock_idx,
        returns_len,
        window=PATTERN_WINDOW,
        horizon=HORIZON,
        index_frame=index_frame,
    )
    assert mask is not None
    # Query is the last bar (index still falling). A rising-era window is the other tape.
    assert not bool(mask[30])
    last_end = returns_len - PATTERN_WINDOW - HORIZON
    assert bool(mask[last_end])


def test_search_analogs_skips_regime_filter_when_too_few_pass() -> None:
    rng = np.random.default_rng(11)
    returns = rng.normal(0.0, 0.01, size=160)
    mask = np.zeros(len(returns), dtype=bool)
    mask[50] = True
    mask[90] = True
    full = search_analogs(returns, window=PATTERN_WINDOW, horizon=HORIZON, max_k=8, min_k=6)
    filtered = search_analogs(
        returns,
        regime_ok=mask,
        window=PATTERN_WINDOW,
        horizon=HORIZON,
        max_k=8,
        min_k=6,
    )
    # Fewer than min_k same-tape windows → keep the unfiltered pool.
    assert len(filtered.pool) == len(full.pool)


def test_search_analogs_applies_regime_filter_when_pool_stays_large() -> None:
    rng = np.random.default_rng(12)
    returns = rng.normal(0.0, 0.01, size=200)
    mask = np.ones(len(returns), dtype=bool)
    mask[50:80] = False
    full = search_analogs(returns, window=PATTERN_WINDOW, horizon=HORIZON, max_k=8, min_k=6)
    filtered = search_analogs(
        returns,
        regime_ok=mask,
        window=PATTERN_WINDOW,
        horizon=HORIZON,
        max_k=8,
        min_k=6,
    )
    assert len(filtered.pool) < len(full.pool)
    assert all(not (50 <= m.end_index < 80) for m in filtered.pool)


def test_forecast_symbol_ignores_regime_frame_for_index() -> None:
    """CSI 300 analog must not be filtered by its own 20-day tape."""
    pattern = [1.004, 1.010, 1.006, 1.012, 1.008] * 4
    rally = [1.02, 1.03, 1.018, 1.028, 1.022] * 2
    closes = [100.0]
    for _ in range(5):
        closes.append(closes[-1])
    for _ in range(4):
        for m in pattern:
            closes.append(closes[-1] * m)
        for m in rally:
            closes.append(closes[-1] * m)
    for m in pattern:
        closes.append(closes[-1] * m)

    frame = _ohlcv_from_closes(closes)
    # Opposite-tape dummy: always falling, would wipe neighbors if applied.
    down = [100.0]
    for _ in range(len(closes) - 1):
        down.append(down[-1] * 0.99)
    regime = _ohlcv_from_closes(down)
    with_filter = forecast_symbol(MARKET_INDEX, frame, regime_frame=regime)
    without = forecast_symbol(MARKET_INDEX, frame)
    assert with_filter["error"] is None
    assert without["error"] is None
    assert with_filter["assessment"]["analog_count"] == without["assessment"]["analog_count"]


def test_trend_rule_index_bull_does_not_treat_analog_as_add_signal() -> None:
    text = _trend_rule("bull").lower()
    assert "do not add" in text
    assert "analog" in text
    assert "veto" in text

