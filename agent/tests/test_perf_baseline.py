"""§8.1 性能基线：四条主链路的耗时上限。

门槛取测试计划的建议目标，而不是本机实测值 —— 实测值随机器和负载漂，把实测值
写成断言只会让 CI 变成"谁跑谁红"。这里断言的是**用户能感知的边界**：监控面板
1.5s 内出、500 个 run 的列表 800ms 内出、50 票 5 年回测 60s 内跑完、20 日 soak
30s 内结算完。实测数字记录在 ``docs/testing/r3-nonfunctional-report.md``。

单独跑：``pytest agent/tests/test_perf_baseline.py -m perf -s``（``-s`` 会打印分位）。
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.store import save_monitor
from src.paper_book.soak import step_range

pytestmark = pytest.mark.perf

BOARD_P95_BUDGET = 1.5      # §8.1 GET /monitor（board）p95
RUNS_BUDGET = 0.8           # §8.1 GET /runs，500 个 run
BACKTEST_BUDGET = 60.0      # §8.1 50 票 × 5 年日线
SOAK_BUDGET = 30.0          # §8.1 20 日 soak


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")


# --------------------------------------------------------------------------
# 监控面板：前端每次刷新都会打这个接口
# --------------------------------------------------------------------------


def test_the_board_endpoint_stays_under_the_p95_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_auth(monkeypatch)
    symbols = [f"{600000 + index}.SH" for index in range(20)]
    save_monitor(
        MonitorConfig(
            strategy=StrategyRef(type="run_dir", run_id="perf-run"),
            suggestions=[
                MonitorSuggestion(
                    symbol=symbol, action="buy", reason="momentum", as_of="2026-11-06"
                )
                for symbol in symbols
            ],
            tradable=True,
        )
    )
    client = _client()
    assert client.get("/monitor").status_code == 200  # warm the route

    samples = []
    for _ in range(100):
        started = time.perf_counter()
        response = client.get("/monitor")
        samples.append(time.perf_counter() - started)
        assert response.status_code == 200

    p95 = statistics.quantiles(samples, n=20)[18]  # 95th of 100 samples
    with capsys.disabled():
        print(f"\n/monitor  p50={statistics.median(samples) * 1000:.1f}ms  p95={p95 * 1000:.1f}ms")
    assert p95 < BOARD_P95_BUDGET


# --------------------------------------------------------------------------
# run 列表：每 500 个 run 就要全目录扫一遍
# --------------------------------------------------------------------------


def _make_run(runs_dir: Path, run_id: str) -> None:
    run_dir = runs_dir / run_id
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    (run_dir / "req.json").write_text('{"prompt": "perf", "context": {}}', encoding="utf-8")
    (run_dir / "code" / "signal_engine.py").write_text("class SignalEngine:\n    pass\n", encoding="utf-8")
    (run_dir / "artifacts" / "equity.csv").write_text(
        "date,equity\n2026-01-05,1000000.0\n", encoding="utf-8"
    )


def test_listing_five_hundred_runs_stays_under_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    for index in range(500):
        _make_run(runs_dir, f"20260101_{index:06d}_01_bt_perf")
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)
    _no_auth(monkeypatch)
    client = _client()

    assert client.get("/runs", params={"limit": 100}).status_code == 200

    started = time.perf_counter()
    response = client.get("/runs", params={"limit": 100})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert len(response.json()) == 100
    with capsys.disabled():
        print(f"\n/runs (500 dirs, limit=100)  {elapsed * 1000:.0f}ms")
    assert elapsed < RUNS_BUDGET


# --------------------------------------------------------------------------
# 回测：50 票 × 5 年日线
# --------------------------------------------------------------------------


def _panel(codes: list[str], sessions: int = 1250) -> dict[str, pd.DataFrame]:
    dates = pd.bdate_range("2021-01-04", periods=sessions)
    panel = {}
    for offset, code in enumerate(codes):
        base = 10.0 + offset * 0.5
        close = [base * (1 + 0.0004 * index + 0.01 * ((index + offset) % 11 - 5) / 5) for index in range(sessions)]
        panel[code] = pd.DataFrame(
            {
                "open": [price * 0.999 for price in close],
                "high": [price * 1.004 for price in close],
                "low": [price * 0.996 for price in close],
                "close": close,
                "volume": [1_000_000.0] * sessions,
            },
            index=dates,
        )
    return panel


class _Loader:
    def __init__(self, panel: dict[str, pd.DataFrame]) -> None:
        self._panel = panel

    def fetch(self, *args, **kwargs):
        return {code: frame.copy() for code, frame in self._panel.items()}


class _Momentum:
    """Cheap but not degenerate: every name flips position every 40 sessions."""

    def generate(self, data_map):
        out = {}
        for code, frame in data_map.items():
            index = frame.index
            out[code] = pd.Series(
                [1.0 if (position // 40) % 2 == 0 else 0.0 for position in range(len(index))],
                index=index,
            )
        return out


def test_fifty_names_over_five_years_fit_in_the_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from backtest.engines.china_a import ChinaAEngine

    codes = [f"{600000 + index}.SH" for index in range(50)]
    panel = _panel(codes)
    config = {
        "codes": codes,
        "start_date": "2021-01-04",
        "end_date": "2025-12-31",
        "source": "local",
        "initial_cash": 10_000_000.0,
    }

    started = time.perf_counter()
    metrics = ChinaAEngine({"initial_cash": 10_000_000.0}).run_backtest(
        config, _Loader(panel), _Momentum(), tmp_path / "runs" / "perf"
    )
    elapsed = time.perf_counter() - started

    assert metrics["trade_count"] > 0
    with capsys.disabled():
        print(f"\nbacktest 50×1250 bars  {elapsed:.2f}s")
    assert elapsed < BACKTEST_BUDGET


# --------------------------------------------------------------------------
# 20 日 soak：逐日结算 + 落盘
# --------------------------------------------------------------------------


def _bars_for_day(day: str, symbols: list[str]) -> dict[str, pd.Series]:
    return {
        symbol: pd.Series(
            {
                "open": 10.0,
                "close": 10.05,
                "pre_close": 10.0,
                "volume": 2_000_000.0,
            }
        )
        for symbol in symbols
    }


def test_a_twenty_session_soak_fits_in_the_budget(capsys: pytest.CaptureFixture[str]) -> None:
    symbols = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH", "000858.SZ"]
    sessions = [day.strftime("%Y-%m-%d") for day in pd.bdate_range("2026-01-05", periods=20)]
    configs = {
        day: MonitorConfig(
            strategy=StrategyRef(type="run_dir", run_id="perf-soak"),
            suggestions=[
                MonitorSuggestion(
                    symbol=symbols[index % len(symbols)],
                    action="buy" if index < len(symbols) else "sell",
                    reason="perf",
                    as_of=day,
                )
            ],
            tradable=True,
        )
        for index, day in enumerate(sessions[:10])
    }
    bars_by_day = {day: _bars_for_day(day, symbols) for day in sessions}

    started = time.perf_counter()
    report = step_range(
        start=sessions[0],
        end=sessions[-1],
        sessions=sessions,
        config=configs[sessions[0]],
        configs_by_day=configs,
        bars_by_day=bars_by_day,
        book_id="perf-soak",
    )
    elapsed = time.perf_counter() - started

    assert report.session_count == 20
    with capsys.disabled():
        print(f"\nsoak 20 sessions  {elapsed:.2f}s")
    assert elapsed < SOAK_BUDGET
