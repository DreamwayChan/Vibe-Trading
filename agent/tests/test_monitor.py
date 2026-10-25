"""Unit tests for the signal monitor board (store + deterministic mapping)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.monitor.models import (
    MAX_SYMBOLS,
    MonitorConfig,
    MonitorSuggestion,
    StrategyRef,
    _normalize_symbols,
)
from src.monitor.scan import (
    MonitorScanError,
    _map_series_to_suggestion,
    reset_csi300_trend_memo,
    run_monitor_scan,
)
from src.monitor.store import (
    board_id_for_strategy,
    board_path,
    load_board_for_strategy,
    load_monitor,
    save_monitor,
)


def test_normalize_symbols_dedupes_and_caps() -> None:
    raw = ["600519.SH", "600519.sh", "000858.SZ"] + [f"S{i}.SZ" for i in range(30)]
    out = _normalize_symbols(raw)
    assert out[0] == "600519.SH"
    assert out.count("600519.SH") == 1
    assert len(out) == MAX_SYMBOLS


def test_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows uses USERPROFILE for Path.home().
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    cfg = MonitorConfig(
        symbols=["600519.SH", "000858.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_abc"),
        schedule="30 7 * * *",
    )
    save_monitor(cfg)
    assert board_path(board_id_for_strategy(cfg.strategy)).is_file()

    loaded = load_monitor()
    assert loaded.symbols == ["600519.SH", "000858.SZ"]
    assert loaded.strategy.run_id == "run_abc"
    assert loaded.schedule == "30 7 * * *"


def test_per_strategy_boards_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each strategy source keeps its own watchlist and scan cache."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    board_a = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_a"),
        suggestions=[
            MonitorSuggestion(
                symbol="AAA.SZ",
                action="buy",
                reason="from A",
                as_of="2024-01-02",
            )
        ],
        last_scan_at="2024-01-02T00:00:00+00:00",
    )
    board_b = MonitorConfig(
        symbols=["BBB.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_b"),
        suggestions=[
            MonitorSuggestion(
                symbol="BBB.SZ",
                action="sell",
                reason="from B",
                as_of="2024-01-03",
            )
        ],
        last_scan_at="2024-01-03T00:00:00+00:00",
    )
    save_monitor(board_a)
    save_monitor(board_b)

    # Active board is the last saved one.
    active = load_monitor()
    assert active.strategy.run_id == "run_b"
    assert active.symbols == ["BBB.SZ"]
    assert active.suggestions[0].action == "sell"

    # Switching back must restore A's symbols/results, not B's.
    restored = load_board_for_strategy(StrategyRef(type="run_dir", run_id="run_a"))
    assert restored.symbols == ["AAA.SZ"]
    assert restored.suggestions[0].reason == "from A"
    assert restored.last_scan_at == "2024-01-02T00:00:00+00:00"


def test_migrate_legacy_default_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from src.monitor.store import monitor_path

    legacy = MonitorConfig(
        symbols=["LEGACY.SZ"],
        strategy=StrategyRef(type="run_dir", run_id="run_legacy"),
    )
    monitor_path().parent.mkdir(parents=True, exist_ok=True)
    monitor_path().write_text(json.dumps(legacy.to_dict()), encoding="utf-8")

    loaded = load_monitor()
    assert loaded.symbols == ["LEGACY.SZ"]
    assert loaded.strategy.run_id == "run_legacy"
    assert board_path("run_run_legacy").is_file()


def test_map_series_buy_sell_hold() -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    buy = _map_series_to_suggestion(
        "AAA",
        pd.Series([0.0, 0.0, 0.5], index=idx),
        "2026-07-24",
    )
    assert buy.action == "buy"
    assert buy.position == "long"
    # Decision date is the scan day, not the bar index.
    assert buy.as_of == "2026-07-24"
    assert buy.data_as_of == "2024-01-03"

    sell = _map_series_to_suggestion(
        "AAA",
        pd.Series([0.5, 0.4, 0.0], index=idx),
        "2026-07-24",
    )
    assert sell.action == "sell"
    assert sell.position == "flat"

    hold_long = _map_series_to_suggestion(
        "AAA",
        pd.Series([0.2, 0.3, 0.4], index=idx),
        "2026-07-24",
    )
    assert hold_long.action == "hold"
    assert hold_long.position == "long"
    assert "stay long" in hold_long.reason

    short = _map_series_to_suggestion(
        "AAA",
        pd.Series([0.0, -0.2, -0.5], index=idx),
        "2026-07-24",
    )
    assert short.position == "short"


def test_run_dir_scan_with_injected_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end scan against a tiny local SignalEngine (no network)."""
    run_id = "20260101_000000_00_test01"
    run_dir = tmp_path / "runs" / run_id
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        out = {}",
                "        for code, df in data_map.items():",
                "            # Last bar long, previous flat → buy",
                "            s = pd.Series(0.0, index=df.index)",
                "            if len(s) >= 1:",
                "                s.iloc[-1] = 1.0",
                "            out[code] = s",
                "        return out",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"source": "auto", "interval": "1D"}),
        encoding="utf-8",
    )

    idx = pd.date_range("2024-06-01", periods=5, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000_000,
        },
        index=idx,
    )

    def fake_fetch_universe(universe, *, start, end, source, interval):
        return {symbol: frame.copy() for symbol in universe}, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)

    cfg = MonitorConfig(
        symbols=["TEST.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    result = run_monitor_scan(cfg, as_of=date(2024, 6, 7), runs_root=tmp_path / "runs")
    assert result.suggestions
    assert result.suggestions[0].symbol == "TEST.SZ"
    assert result.suggestions[0].action == "buy"
    assert result.suggestions[0].position == "long"
    assert result.suggestions[0].as_of == "2024-06-07"
    assert result.suggestions[0].price == 10.5
    assert result.last_scan_at


def test_run_dir_scan_stamps_holding_trend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holdings pick up the dual-track tape without feeding CSI 300 into generate."""
    import numpy as np

    run_id = "20260101_000000_00_trend01"
    run_dir = tmp_path / "runs" / run_id
    code_dir = run_dir / "code"
    code_dir.mkdir(parents=True)
    (code_dir / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        if '000300.SH' in data_map:",
                "            raise AssertionError('CSI 300 must not enter generate')",
                "        out = {}",
                "        for code, df in data_map.items():",
                "            s = pd.Series(0.0, index=df.index)",
                "            if len(s):",
                "                s.iloc[-1] = 0.5",
                "            out[code] = s",
                "        return out",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"source": "auto", "interval": "1D", "codes": ["AAA.SZ"]}),
        encoding="utf-8",
    )

    idx = pd.bdate_range("2023-01-03", periods=200)
    bull = pd.DataFrame(
        {
            "open": [10.0 + i * 0.05 for i in range(200)],
            "high": [10.2 + i * 0.05 for i in range(200)],
            "low": [9.8 + i * 0.05 for i in range(200)],
            "close": [10.0 + i * 0.05 for i in range(200)],
            "volume": 1_000_000,
        },
        index=idx,
    )
    chop_close = 20.0 + np.sin(np.linspace(0, 12 * np.pi, 200))
    chop = pd.DataFrame(
        {
            "open": chop_close,
            "high": chop_close + 0.2,
            "low": chop_close - 0.2,
            "close": chop_close,
            "volume": 1_000_000,
        },
        index=idx,
    )

    def fake_fetch_universe(universe, *, start, end, source, interval):
        out = {}
        for symbol in universe:
            out[symbol] = chop.copy() if symbol == "000300.SH" else bull.copy()
        return out, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda data_map, *, as_of_date: (
            data_map,
            {k: False for k in data_map},
            {},
        ),
    )
    reset_csi300_trend_memo()

    cfg = MonitorConfig(
        symbols=["AAA.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    result = run_monitor_scan(
        cfg, as_of=idx[-1].date(), runs_root=tmp_path / "runs"
    )
    assert len(result.holdings) == 1
    trend = result.holdings[0].trend
    assert trend is not None
    assert trend["action"] == "hold"
    assert trend["stock_regime"] == "bull"
    assert trend["market_regime"] == "chop"


def test_second_strategy_scan_reuses_csi300_without_index_daily(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching boards must not spend another hourly index_daily call."""
    import numpy as np

    from src.monitor.trend_regime import CSI300

    idx = pd.bdate_range("2023-01-03", periods=200)
    as_of = idx[-1].date()
    bull = pd.DataFrame(
        {
            "open": [10.0 + i * 0.05 for i in range(200)],
            "high": [10.2 + i * 0.05 for i in range(200)],
            "low": [9.8 + i * 0.05 for i in range(200)],
            "close": [10.0 + i * 0.05 for i in range(200)],
            "volume": 1_000_000,
        },
        index=idx,
    )
    chop_close = 20.0 + np.sin(np.linspace(0, 12 * np.pi, 200))
    chop = pd.DataFrame(
        {
            "open": chop_close,
            "high": chop_close + 0.2,
            "low": chop_close - 0.2,
            "close": chop_close,
            "volume": 1_000_000,
        },
        index=idx,
    )

    engine_src = "\n".join(
        [
            "import pandas as pd",
            "class SignalEngine:",
            "    def generate(self, data_map):",
            "        out = {}",
            "        for code, df in data_map.items():",
            "            s = pd.Series(0.0, index=df.index)",
            "            if len(s):",
            "                s.iloc[-1] = 0.5",
            "            out[code] = s",
            "        return out",
        ]
    )
    csi_ends: list[str] = []

    def _write_run(run_id: str) -> None:
        run_dir = tmp_path / "runs" / run_id
        code_dir = run_dir / "code"
        code_dir.mkdir(parents=True)
        (code_dir / "signal_engine.py").write_text(engine_src, encoding="utf-8")
        (run_dir / "config.json").write_text(
            json.dumps({"source": "auto", "interval": "1D", "codes": ["AAA.SZ"]}),
            encoding="utf-8",
        )

    def fake_fetch_universe(universe, *, start, end, source, interval):
        if list(universe) == [CSI300]:
            csi_ends.append(end)
            if len(csi_ends) > 1:
                return {}, {CSI300: "index_daily hourly quota"}
            return {CSI300: chop.copy()}, {}
        return {symbol: bull.copy() for symbol in universe}, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda data_map, *, as_of_date: (
            data_map,
            {k: False for k in data_map},
            {},
        ),
    )
    reset_csi300_trend_memo()
    _write_run("run_board_a")
    _write_run("run_board_b")

    first = run_monitor_scan(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_board_a"),
        ),
        as_of=as_of,
        runs_root=tmp_path / "runs",
    )
    second = run_monitor_scan(
        MonitorConfig(
            symbols=["AAA.SZ"],
            strategy=StrategyRef(type="run_dir", run_id="run_board_b"),
        ),
        as_of=as_of,
        runs_root=tmp_path / "runs",
    )
    assert first.holdings[0].trend is not None
    assert first.holdings[0].trend["action"] == "hold"
    assert second.holdings[0].trend is not None
    assert second.holdings[0].trend["action"] == "hold"
    assert csi_ends == [(as_of - timedelta(days=1)).isoformat()]


def test_scan_requires_strategy_pointer() -> None:
    with pytest.raises(MonitorScanError, match="run_id is required"):
        run_monitor_scan(MonitorConfig())


def test_fetch_universe_ohlcv_batches_all_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monitor must send the full universe in one fetch_data_map call."""
    from types import SimpleNamespace

    from src.monitor.scan import _fetch_universe_ohlcv

    calls: list[tuple[list[str], bool]] = []
    idx = pd.date_range("2024-06-01", periods=2, freq="B")
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
        index=idx,
    )

    def fake_fetch_data_map(config: dict, *, allow_partial: bool = False):
        codes = list(config["codes"])
        calls.append((codes, allow_partial))
        return SimpleNamespace(data_map={code: frame.copy() for code in codes})

    monkeypatch.setattr("backtest.runner.fetch_data_map", fake_fetch_data_map)
    data_map, errors = _fetch_universe_ohlcv(
        ["000630.SZ", "000737.SZ", "601899.SH"],
        start="2024-01-01",
        end="2024-06-01",
        source="tushare",
        interval="1D",
    )
    assert calls == [(["000630.SZ", "000737.SZ", "601899.SH"], True)]
    assert set(data_map) == {"000630.SZ", "000737.SZ", "601899.SH"}
    assert errors == {}
