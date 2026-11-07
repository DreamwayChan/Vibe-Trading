"""End-to-end smoke test: ChinaAEngine on 5-minute bars.

Intraday bars reach the engine through the same path as daily bars, but the
things that break are different: the T+1 rule has to compare *dates* rather
than bar labels, and annualisation has to know a session is 48 bars long. This
test drives a real ``ChinaAEngine`` run on in-memory minute bars (no network)
and checks the run completes with finite metrics.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engines.china_a import ChinaAEngine
from backtest.metrics import calc_bars_per_year


def _intraday_bars(sessions: tuple[str, ...] = ("2024-01-02", "2024-01-03", "2024-01-04")) -> pd.DataFrame:
    stamps = []
    for day in sessions:
        stamps += list(pd.date_range(f"{day} 09:35", f"{day} 11:30", freq="5min"))
        stamps += list(pd.date_range(f"{day} 13:05", f"{day} 15:00", freq="5min"))
    index = pd.DatetimeIndex(stamps)
    # A gentle uptrend so the long-only signal has something to earn.
    close = np.linspace(10.0, 11.5, len(index))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 100_000.0,
        },
        index=index,
    )


class _FakeLoader:
    def __init__(self, code: str, bars: pd.DataFrame) -> None:
        self._code = code
        self._bars = bars

    def fetch(self, *args, **kwargs):
        return {self._code: self._bars.copy()}


class _LongAfterFirstSession:
    """Flat for the first session, fully long afterwards."""

    def __init__(self, code: str, first_session_bars: int = 48) -> None:
        self._code = code
        self._warmup = first_session_bars

    def generate(self, data_map):
        index = data_map[self._code].index
        weights = pd.Series(0.0, index=index)
        weights.iloc[self._warmup :] = 1.0
        return {self._code: weights}


def test_intraday_backtest_completes(tmp_path: Path) -> None:
    code = "600519.SH"
    bars = _intraday_bars()
    engine = ChinaAEngine({"commission": 0.00025, "slippage": 0.001})

    result = engine.run_backtest(
        {
            "codes": [code],
            "start_date": "2024-01-02",
            "end_date": "2024-01-04",
            "source": "lake",
            "interval": "5m",
            "initial_cash": 1_000_000,
        },
        _FakeLoader(code, bars),
        _LongAfterFirstSession(code),
        tmp_path / "run",
        bars_per_year=calc_bars_per_year("5m", "lake"),
    )

    metrics = result["metrics"] if isinstance(result, dict) and "metrics" in result else result
    for key in ("total_return", "sharpe", "max_drawdown"):
        assert key in metrics, f"missing metric {key}"
        assert math.isfinite(float(metrics[key])), f"{key} is not finite"


def test_intraday_t_plus_one_blocks_same_day_exit() -> None:
    """A position opened today cannot be sold later the same session."""
    bars = _intraday_bars()
    engine = ChinaAEngine({})
    engine.positions.clear()

    first_bar = bars.iloc[0]
    engine.can_execute("600519.SH", 1, first_bar)  # buy allowed

    from backtest.models import Position

    engine.positions["600519.SH"] = Position(
        symbol="600519.SH",
        direction=1,
        entry_price=float(first_bar["close"]),
        entry_time=bars.index[0],
        size=100.0,
    )
    later_same_day = bars.iloc[10]
    assert engine.can_execute("600519.SH", 0, later_same_day) is False

    next_day = bars.iloc[60]
    assert engine.can_execute("600519.SH", 0, next_day) is True
