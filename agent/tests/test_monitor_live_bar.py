"""Tests for provisional same-day bar overlay used by the monitor board."""

from __future__ import annotations

import warnings
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.monitor import live_bar
from src.monitor.live_bar import LiveQuote, ensure_same_day_bar, fetch_live_quotes
from src.monitor.models import MonitorConfig, StrategyRef
from src.monitor.scan import run_monitor_scan


def _ohlcv(days: list[str], close: float = 10.0) -> pd.DataFrame:
    idx = pd.to_datetime(days)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _reset_live_quote_state() -> None:
    live_bar.reset_live_quote_state_for_tests()
    yield
    live_bar.reset_live_quote_state_for_tests()


def test_ensure_same_day_bar_appends_when_history_lags() -> None:
    frame = _ohlcv(["2026-07-22", "2026-07-23"], close=28.5)
    quote = LiveQuote(last=28.91, open=28.6, high=29.0, low=28.4, volume=2e6)
    out, provisional = ensure_same_day_bar(frame, quote, date(2026, 7, 24))
    assert provisional is True
    assert out.index[-1].date() == date(2026, 7, 24)
    assert float(out.iloc[-1]["close"]) == pytest.approx(28.91)
    assert float(out.iloc[-1]["open"]) == pytest.approx(28.6)
    assert len(out) == 3


def test_ensure_same_day_bar_appends_extra_columns_without_concat_warning() -> None:
    """Tushare frames carry amount; appending today must not FutureWarn."""
    frame = _ohlcv(["2026-07-22", "2026-07-23"], close=28.5)
    frame["amount"] = [100.0, 110.0]
    quote = LiveQuote(last=28.91, open=28.6, high=29.0, low=28.4, volume=2e6)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        out, provisional = ensure_same_day_bar(frame, quote, date(2026, 7, 24))
    assert provisional is True
    assert "amount" in out.columns
    assert pd.isna(out.iloc[-1]["amount"])
    assert float(out.iloc[0]["amount"]) == pytest.approx(100.0)
    assert float(out.iloc[-1]["close"]) == pytest.approx(28.91)


def test_ensure_same_day_bar_refreshes_when_already_current() -> None:
    """Vendor same-day bars must be overwritten so rescans stay realtime."""
    frame = _ohlcv(["2026-07-23", "2026-07-24"], close=28.5)
    quote = LiveQuote(last=99.0, open=98.0, high=100.0, low=97.0, volume=3e6)
    out, provisional = ensure_same_day_bar(frame, quote, date(2026, 7, 24))
    assert provisional is True
    assert len(out) == 2
    assert float(out.iloc[-1]["close"]) == pytest.approx(99.0)
    assert float(out.iloc[-1]["open"]) == pytest.approx(98.0)
    assert float(out.iloc[-1]["high"]) == pytest.approx(100.0)
    assert float(out.iloc[-1]["low"]) == pytest.approx(97.0)
    # Prior bars stay untouched.
    assert float(out.iloc[0]["close"]) == pytest.approx(28.5)


def test_ensure_same_day_bar_noop_without_quote() -> None:
    frame = _ohlcv(["2026-07-23"], close=28.5)
    out, provisional = ensure_same_day_bar(frame, None, date(2026, 7, 24))
    assert provisional is False
    assert len(out) == 1


def test_fetch_live_quotes_falls_back_to_tencent_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom_eastmoney(symbol: str):
        live_bar._eastmoney_record_failure(RuntimeError("proxy down"))
        return None

    def fake_tencent(symbols: list[str]):
        return {
            s: LiveQuote(last=12.3, open=12.0, high=12.5, low=11.9, source="tencent")
            for s in symbols
        }

    monkeypatch.setattr(live_bar, "_quote_eastmoney", boom_eastmoney)
    monkeypatch.setattr(live_bar, "_quote_tencent_batch", fake_tencent)
    monkeypatch.setattr(live_bar, "_quote_sina_batch", lambda symbols: {})

    out = fetch_live_quotes(["002262.SZ", "300016.SZ"])
    assert out["002262.SZ"] is not None
    assert out["002262.SZ"].source == "tencent"
    assert out["300016.SZ"] is not None


def test_eastmoney_circuit_skips_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def always_fail(symbol: str):
        calls["n"] += 1
        live_bar._eastmoney_record_failure(RuntimeError("proxy down"))
        return None

    monkeypatch.setattr(live_bar, "_quote_eastmoney", always_fail)
    monkeypatch.setattr(live_bar, "_quote_tencent_batch", lambda symbols: {})
    monkeypatch.setattr(live_bar, "_quote_sina_batch", lambda symbols: {})

    # Trip the breaker with the first batch of failures.
    for _ in range(live_bar._EASTMONEY_FAIL_THRESHOLD):
        fetch_live_quotes([f"00000{_}.SZ"])

    before = calls["n"]
    fetch_live_quotes(["600000.SH", "000001.SZ"])
    # Circuit open → Eastmoney not consulted for the second round.
    assert calls["n"] == before
    assert live_bar._eastmoney_is_open()


def test_fetch_live_quotes_prefers_healthy_tencent_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Tencent succeeds and Eastmoney is cooling, try Tencent first."""
    order: list[str] = []

    def track_eastmoney(symbol: str):
        order.append("eastmoney")
        return None

    def track_tencent(symbols: list[str]):
        order.append("tencent")
        return {
            s: LiveQuote(last=1.0, open=1.0, high=1.0, low=1.0, source="tencent")
            for s in symbols
        }

    # Trip Eastmoney + boost Tencent so prefer_order leads with Tencent.
    for _ in range(live_bar._EASTMONEY_FAIL_THRESHOLD):
        live_bar._eastmoney_record_failure(RuntimeError("proxy down"))
    from backtest.loaders.source_health import record_success

    record_success("tencent")

    monkeypatch.setattr(live_bar, "_quote_eastmoney", track_eastmoney)
    monkeypatch.setattr(live_bar, "_quote_tencent_batch", track_tencent)
    monkeypatch.setattr(live_bar, "_quote_sina_batch", lambda symbols: {})

    out = fetch_live_quotes(["002262.SZ"])
    assert out["002262.SZ"] is not None
    assert out["002262.SZ"].source == "tencent"
    assert order[0] == "tencent"
    assert "eastmoney" not in order


def test_parse_tencent_batch_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def _tencent_payload(
        *,
        code: str,
        name: str,
        last: str,
        open_px: str,
        high: str,
        low: str,
        volume: str,
    ) -> str:
        # Layout used by _quote_tencent_batch: [3]=last [5]=open [6]=vol
        # [33]=high [34]=low — pad unused slots so indices line up.
        fields = [""] * 35
        fields[1] = name
        fields[2] = code
        fields[3] = last
        fields[5] = open_px
        fields[6] = volume
        fields[33] = high
        fields[34] = low
        return "~".join(fields)

    body = (
        f'v_sz002262="{_tencent_payload(code="002262", name="恩华药业", last="28.91", open_px="28.60", high="29.00", low="28.40", volume="100")}";\n'
        f'v_sz300016="{_tencent_payload(code="300016", name="北陆药业", last="10.50", open_px="10.20", high="10.60", low="10.10", volume="200")}";'
    )

    class _Resp:
        content = body.encode("gbk")

    monkeypatch.setattr(
        live_bar,
        "_direct_session",
        lambda: SimpleNamespace(get=lambda *a, **k: _Resp()),
    )
    hits = live_bar._quote_tencent_batch(["002262.SZ", "300016.SZ"])
    assert set(hits) == {"002262.SZ", "300016.SZ"}
    assert hits["002262.SZ"].last == pytest.approx(28.91)
    assert hits["002262.SZ"].high == pytest.approx(29.0)
    assert hits["002262.SZ"].low == pytest.approx(28.4)


def test_scan_marks_provisional_when_overlay_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "20260101_000000_00_prov01"
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
        '{"source": "auto", "interval": "1D"}',
        encoding="utf-8",
    )

    hist = _ohlcv(["2026-07-21", "2026-07-22", "2026-07-23"], close=10.0)

    def fake_fetch_universe(universe, *, start, end, source, interval):
        return {symbol: hist.copy() for symbol in universe}, {}

    monkeypatch.setattr("src.monitor.scan._fetch_universe_ohlcv", fake_fetch_universe)
    monkeypatch.setattr(
        "src.monitor.live_bar.fetch_live_quotes",
        lambda symbols: {
            str(s).upper(): LiveQuote(last=10.5, open=10.2, high=10.6, low=10.1)
            for s in symbols
        },
    )

    cfg = MonitorConfig(
        symbols=["TEST.SZ"],
        strategy=StrategyRef(type="run_dir", run_id=run_id),
    )
    result = run_monitor_scan(cfg, as_of=date(2026, 7, 24), runs_root=tmp_path / "runs")
    assert result.suggestions
    row = result.suggestions[0]
    assert row.provisional is True
    assert row.as_of == "2026-07-24"
    assert row.data_as_of == "2026-07-24"
    assert row.price == pytest.approx(10.5)
    assert row.action == "buy"
    assert "[provisional same-day]" in row.reason
