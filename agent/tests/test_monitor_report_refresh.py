"""Tests for monitor → bound-run report refresh."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.monitor.report_refresh import (
    extend_run_end_date,
    refresh_bound_run_report,
)
from src.monitor.scan import run_monitor_scan
from src.monitor.models import MonitorConfig, StrategyRef


def _write_minimal_run(run_dir: Path, *, end_date: str = "2024-06-01") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "code").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["600519.SH"],
                "start_date": "2024-01-01",
                "end_date": end_date,
                "source": "tushare",
                "interval": "1D",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "req.json").write_text(
        json.dumps(
            {
                "prompt": "demo",
                "context": {
                    "codes": ["600519.SH"],
                    "start_date": "2024-01-01",
                    "end_date": end_date,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n"
        "    def generate(self, data_map):\n"
        "        return {}\n",
        encoding="utf-8",
    )
    # Stale hand-written series that must be removed so ohlcv_* wins after refresh.
    (run_dir / "artifacts" / "price_series.csv").write_text(
        "time,code,open,high,low,close,volume\n"
        "2024-01-02,600519.SH,1,1,1,1,1\n",
        encoding="utf-8",
    )


def test_extend_run_end_date_forwards_config_and_req(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_copper"
    _write_minimal_run(run_dir, end_date="2024-06-01")

    info = extend_run_end_date(run_dir, date(2024, 8, 12))
    assert info["changed"] is True
    assert info["previous_end"] == "2024-06-01"
    assert info["end_date"] == "2024-08-12"

    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    req = json.loads((run_dir / "req.json").read_text(encoding="utf-8"))
    assert config["end_date"] == "2024-08-12"
    assert req["context"]["end_date"] == "2024-08-12"


def test_extend_run_end_date_refuses_shrink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_keep"
    _write_minimal_run(run_dir, end_date="2024-12-01")

    info = extend_run_end_date(run_dir, date(2024, 8, 12))
    assert info["changed"] is False
    assert info["reason"] == "as_of_before_existing_end"
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert config["end_date"] == "2024-12-01"


def test_refresh_bound_run_report_rebenches_and_stamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_MONITOR_REFRESH_REPORT", "1")
    from src.config.accessor import reset_env_config

    reset_env_config()

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run_abc"
    _write_minimal_run(run_dir, end_date="2024-06-01")

    calls: list[str] = []

    def fake_backtest(run_dir_str: str) -> str:
        calls.append(run_dir_str)
        # Simulate engine rewriting OHLCV after the refresh.
        artifacts = Path(run_dir_str) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "ohlcv_600519.SH.csv").write_text(
            "trade_date,open,high,low,close,volume\n"
            "2024-08-12,10,11,9,10.5,100\n",
            encoding="utf-8",
        )
        return json.dumps({"status": "ok", "exit_code": 0, "artifacts": {}})

    result = refresh_bound_run_report(
        "run_abc",
        date(2024, 8, 12),
        runs_root=runs_root,
        run_backtest_fn=fake_backtest,
    )
    assert result["ok"] is True
    assert calls == [str(run_dir)]
    assert not (run_dir / "artifacts" / "price_series.csv").exists()
    stamp = json.loads(
        (run_dir / "artifacts" / "monitor_refresh.json").read_text(encoding="utf-8")
    )
    assert stamp["status"] == "ok"
    assert stamp["as_of"] == "2024-08-12"
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert config["end_date"] == "2024-08-12"

    reset_env_config()


def test_refresh_skipped_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_MONITOR_REFRESH_REPORT", "0")
    from src.config.accessor import reset_env_config

    reset_env_config()
    runs_root = tmp_path / "runs"
    _write_minimal_run(runs_root / "run_abc")

    result = refresh_bound_run_report(
        "run_abc",
        date(2024, 8, 12),
        runs_root=runs_root,
        run_backtest_fn=lambda _p: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert result["skipped"] is True
    assert result["reason"] == "disabled_by_env"
    reset_env_config()


def test_run_monitor_scan_triggers_report_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scan with refresh_report=True should call report refresh for run_dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("VIBE_TRADING_MONITOR_REFRESH_REPORT", "1")
    from src.config.accessor import reset_env_config

    reset_env_config()

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "run_xyz"
    _write_minimal_run(run_dir)

    refresh_calls: list[tuple[str, date]] = []

    def fake_refresh(run_id: str, as_of: date, *, runs_root=None, run_backtest_fn=None):
        refresh_calls.append((run_id, as_of))
        return {"ok": True, "run_id": run_id}

    monkeypatch.setattr(
        "src.monitor.report_refresh.refresh_bound_run_report",
        fake_refresh,
    )

    # Bypass the heavy OHLCV / SignalEngine path: stub _scan_run_dir.
    from src.monitor.models import DayOps, MonitorSuggestion

    def fake_scan_run_dir(config, *, as_of_date, lookback_days, runs_root):
        sug = [
            MonitorSuggestion(
                symbol="600519.SH",
                action="hold",
                reason="stub",
                as_of=as_of_date.isoformat(),
            )
        ]
        return sug, [], DayOps(), {"universe_size": 1, "fetch_error_count": 0}

    monkeypatch.setattr("src.monitor.scan._scan_run_dir", fake_scan_run_dir)

    cfg = MonitorConfig(
        symbols=["600519.SH"],
        strategy=StrategyRef(type="run_dir", run_id="run_xyz"),
    )
    out = run_monitor_scan(
        cfg,
        as_of=date(2024, 8, 12),
        runs_root=runs_root,
        refresh_report=True,
    )
    assert out.last_scan_at
    assert refresh_calls == [("run_xyz", date(2024, 8, 12))]
    reset_env_config()


def test_merge_ohlcv_keeps_early_history() -> None:
    from src.monitor.report_refresh import _merge_ohlcv

    base = {
        "AAA.SZ": pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
    }
    incoming = {
        "AAA.SZ": pd.DataFrame(
            {"close": [2.5, 3.0]},
            index=pd.to_datetime(["2024-01-03", "2024-01-04"]),
        )
    }
    merged = _merge_ohlcv(base, incoming)
    assert list(merged["AAA.SZ"].index.strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert float(merged["AAA.SZ"].loc["2024-01-03", "close"]) == 2.5


def test_is_truncated_detects_short_panel() -> None:
    from src.monitor.report_refresh import _is_truncated

    assert _is_truncated("2025-02-28", "2024-01-01") is True
    assert _is_truncated("2024-01-10", "2024-01-01") is False


def test_run_monitor_scan_skips_report_refresh_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchecked / default scan must not re-bench the bound run."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    runs_root = tmp_path / "runs"
    _write_minimal_run(runs_root / "run_xyz")

    def boom(*_a, **_k):
        raise AssertionError("refresh_bound_run_report must not run")

    monkeypatch.setattr("src.monitor.report_refresh.refresh_bound_run_report", boom)

    from src.monitor.models import DayOps, MonitorSuggestion

    def fake_scan_run_dir(config, *, as_of_date, lookback_days, runs_root):
        return (
            [
                MonitorSuggestion(
                    symbol="600519.SH",
                    action="hold",
                    reason="stub",
                    as_of=as_of_date.isoformat(),
                )
            ],
            [],
            DayOps(),
            {"universe_size": 1, "fetch_error_count": 0},
        )

    monkeypatch.setattr("src.monitor.scan._scan_run_dir", fake_scan_run_dir)

    cfg = MonitorConfig(
        symbols=["600519.SH"],
        strategy=StrategyRef(type="run_dir", run_id="run_xyz"),
    )
    out = run_monitor_scan(cfg, as_of=date(2024, 8, 12), runs_root=runs_root)
    assert out.last_scan_at
