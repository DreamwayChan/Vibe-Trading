"""§8.3 对账：从产物重算的 validation 必须与引擎写出的一致。

引擎在跑回测时把统计量写进 ``artifacts/validation.json``；事后审计拿的只有
``equity.csv`` + ``trades.csv`` + ``config.json``。如果重算结果对不上，只有两种
可能：要么产物不足以支撑结论（审计无效），要么口径漂了（数字好看但不可复现）。
两种都不允许静默存在，所以这里断言的是**逐字节相等**，不是"差不多"。

历史上这里确实对不上，原因有三个，本文件各有一条用例钉住：

1. CLI 用函数默认值（1000 次模拟、5 个窗口）而不是 run 自己的 ``validation`` 参数；
2. 年化因子在产物里没有落盘，重算只能猜 252；
3. ``trades.csv`` 一行一笔成交，旧实现直接拿卖出行的 ``timestamp`` 当建仓时间，
   walk-forward 按 ``entry_time`` 分窗，跨窗的交易就被记到了错误的窗口。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtest import validation
from backtest.engines.china_a import ChinaAEngine

SYMBOL = "600519.SH"
TRADES_HEADER = (
    "timestamp,code,side,price,qty,reason,pnl,holding_days,return_pct\n"
)


def _bars(sessions: int = 120) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=sessions)
    close = [100.0 + index * 0.5 + (index % 7) * 0.3 for index in range(sessions)]
    return pd.DataFrame(
        {
            "open": [price - 0.2 for price in close],
            "high": [price + 0.4 for price in close],
            "low": [price - 0.5 for price in close],
            "close": close,
            "volume": [500_000.0] * sessions,
        },
        index=dates,
    )


class _Loader:
    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars

    def fetch(self, *args, **kwargs):
        return {SYMBOL: self._bars.copy()}


class _FlipSignal:
    """Flips position every 10 bars, so the run produces several closed trades."""

    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars

    def generate(self, data_map):
        index = data_map[SYMBOL].index
        values = [1.0 if (position // 10) % 2 == 0 else 0.0 for position in range(len(index))]
        return {SYMBOL: pd.Series(values, index=index)}


def _run_config(bars: pd.DataFrame, *, knobs: dict | None = None) -> dict:
    config = {
        "codes": [SYMBOL],
        "start_date": bars.index[0].strftime("%Y-%m-%d"),
        "end_date": bars.index[-1].strftime("%Y-%m-%d"),
        "source": "local",
        "interval": "1D",
        "initial_cash": 1_000_000.0,
    }
    if knobs is not None:
        config["validation"] = knobs
    return config


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A real run whose validation.json was written by the engine.

    Knobs are deliberately non-default (300 simulations, 3 windows) — with the
    defaults a CLI that ignored the config would still look correct.
    """
    bars = _bars()
    config = _run_config(
        bars,
        knobs={
            "monte_carlo": {"n_simulations": 300, "seed": 7},
            "bootstrap": {"n_bootstrap": 300, "seed": 7},
            "walk_forward": {"n_windows": 3},
        },
    )
    out = tmp_path / "runs" / "reconcile"
    ChinaAEngine({"initial_cash": 1_000_000.0}).run_backtest(
        config, _Loader(bars), _FlipSignal(bars), out
    )
    # The runner persists the resolved config beside the artifacts; the engine
    # alone does not, and validation reads it for cash/costs/knobs.
    (out / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return out


def _validation_path(run_dir: Path) -> Path:
    return run_dir / "artifacts" / "validation.json"


def test_recompute_reproduces_the_engine_artifact_byte_for_byte(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The audit path must land on the same numbers the gate decided on."""
    engine_bytes = _validation_path(run_dir).read_bytes()

    validation.main(run_dir)
    capsys.readouterr()

    assert _validation_path(run_dir).read_bytes() == engine_bytes


def test_recompute_is_idempotent(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running the audit twice must not drift — otherwise "last run wins"."""
    validation.main(run_dir)
    capsys.readouterr()
    first = _validation_path(run_dir).read_bytes()

    validation.main(run_dir)
    capsys.readouterr()

    assert _validation_path(run_dir).read_bytes() == first


def test_recompute_uses_the_runs_own_simulation_knobs(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 300-simulation run rechecked at 1000 is a different experiment."""
    results = validation.main(run_dir)
    capsys.readouterr()

    assert results["monte_carlo"]["n_simulations"] == 300
    assert results["bootstrap"]["n_bootstrap"] == 300
    assert results["walk_forward"]["n_windows"] == 3


def test_the_recorded_bars_per_year_is_reused(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The annualisation factor cannot be re-derived from the CSVs alone.

    A mixed-market run deliberately passes ``None`` (calendar-day
    annualisation); a monthly-bar run uses 12. Recomputing at 252 would move
    every Sharpe ratio without a single bar changing.
    """
    original = json.loads(_validation_path(run_dir).read_text(encoding="utf-8"))
    original["bars_per_year"] = 12
    _validation_path(run_dir).write_text(json.dumps(original), encoding="utf-8")

    results = validation.main(run_dir)
    capsys.readouterr()

    assert results["bars_per_year"] == 12
    assert results["bootstrap"]["observed_sharpe"] != original["bootstrap"]["observed_sharpe"]


def test_tampering_with_the_equity_artifact_changes_the_recomputed_numbers(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guard against a vacuous comparison: the numbers must be data-dependent.

    The tamper is a *shape* change, not a rescale — every statistic here is
    computed from returns, so multiplying the whole curve by a constant is
    invisible to it by design.
    """
    before = validation.main(run_dir)
    capsys.readouterr()

    equity_path = run_dir / "artifacts" / "equity.csv"
    frame = pd.read_csv(equity_path, index_col=0)
    half = len(frame) // 2
    frame.loc[frame.index[half:], "equity"] = frame["equity"].iloc[half:] * 0.9
    frame.to_csv(equity_path)

    after = validation.main(run_dir)
    capsys.readouterr()

    assert after["walk_forward"]["return_mean"] != before["walk_forward"]["return_mean"]
    # Window 2 straddles the tampered boundary; window 1 is entirely before it
    # and must stay put — that is how an auditor localises the damage.
    assert after["walk_forward"]["windows"][1]["return"] != before["walk_forward"]["windows"][1]["return"]
    assert after["walk_forward"]["windows"][0]["return"] == before["walk_forward"]["windows"][0]["return"]


def test_entry_time_comes_from_the_buy_row_not_the_sell_row(tmp_path: Path) -> None:
    """Walk-forward attributes trades by entry_time; pairing must be real."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "trades.csv").write_text(
        TRADES_HEADER
        + "2025-01-06,600519.SH,buy,100.0,1000,signal,0,0,0\n"
        + "2025-03-10,600519.SH,sell,110.0,1000,signal,10000,42,10.0\n",
        encoding="utf-8",
    )

    trades = validation._load_trades(tmp_path)

    assert len(trades) == 1
    assert trades[0].entry_time == pd.Timestamp("2025-01-06")
    assert trades[0].exit_time == pd.Timestamp("2025-03-10")
    assert trades[0].entry_price == pytest.approx(100.0)
    assert trades[0].exit_price == pytest.approx(110.0)
    assert trades[0].holding_bars == 42


def test_break_even_exits_are_still_closed_trades(tmp_path: Path) -> None:
    """A pnl of exactly 0 is a flat trade, not an open position.

    The old filter was ``pnl != 0``, which silently dropped these — shifting
    n_trades, the win rate and every Monte Carlo input.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "trades.csv").write_text(
        TRADES_HEADER
        + "2025-01-06,600519.SH,buy,100.0,1000,signal,0,0,0\n"
        + "2025-01-10,600519.SH,sell,100.0,1000,signal,0,4,0.0\n",
        encoding="utf-8",
    )

    trades = validation._load_trades(tmp_path)

    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(0.0)


def test_each_sell_pairs_with_its_own_buy(tmp_path: Path) -> None:
    """Two round trips must not share an entry, and the second must not inherit
    the first one's date — that is what FIFO pairing buys."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "trades.csv").write_text(
        TRADES_HEADER
        + "2025-01-06,600519.SH,buy,100.0,1000,signal,0,0,0\n"
        + "2025-01-20,600519.SH,sell,105.0,1000,signal,5000,10,5.0\n"
        + "2025-02-03,600519.SH,buy,106.0,1000,signal,0,0,0\n"
        + "2025-02-17,600519.SH,sell,112.0,1000,signal,6000,10,5.7\n",
        encoding="utf-8",
    )

    trades = validation._load_trades(tmp_path)

    assert [str(trade.entry_time.date()) for trade in trades] == ["2025-01-06", "2025-02-03"]
    assert [trade.entry_price for trade in trades] == pytest.approx([100.0, 106.0])


def test_a_sell_without_a_matching_buy_falls_back_to_its_own_timestamp(tmp_path: Path) -> None:
    """Hand-edited or truncated CSVs must not crash the audit."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "trades.csv").write_text(
        TRADES_HEADER + "2025-03-10,600519.SH,sell,110.0,1000,signal,10000,42,10.0\n",
        encoding="utf-8",
    )

    trades = validation._load_trades(tmp_path)

    assert len(trades) == 1
    assert trades[0].entry_time == pd.Timestamp("2025-03-10")
    assert trades[0].entry_price == 0.0
