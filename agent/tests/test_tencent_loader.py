"""Tests for Tencent's daily-only market-data contract."""

from __future__ import annotations

import pandas as pd

from backtest.loaders import tencent_loader


def test_intraday_request_does_not_return_daily_bars(monkeypatch) -> None:
    calls: list[str] = []
    daily = pd.DataFrame(
        {
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [100.0],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-05")]),
    )
    loader = tencent_loader.DataLoader()
    monkeypatch.setattr(
        tencent_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )
    monkeypatch.setattr(
        loader,
        "_fetch_one",
        lambda code, start, end: calls.append(code) or daily,
    )

    result = loader.fetch(
        ["600519.SH"],
        "2026-01-01",
        "2026-01-31",
        interval="1m",
    )

    assert result == {}
    assert calls == []


def test_tencent_skips_b_share_and_index_without_http(monkeypatch) -> None:
    calls: list[str] = []
    loader = tencent_loader.DataLoader()
    monkeypatch.setattr(
        tencent_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )
    monkeypatch.setattr(
        loader,
        "_fetch_one",
        lambda code, start, end: calls.append(code),
    )
    result = loader.fetch(
        ["900952.SH", "200771.SZ", "000300.SH"],
        "2026-01-01",
        "2026-01-31",
        interval="1D",
    )
    assert result == {}
    assert calls == []


def test_tencent_stops_remaining_after_hard_501(monkeypatch) -> None:
    from backtest.loaders.source_health import reset_for_tests

    reset_for_tests()
    calls: list[str] = []
    loader = tencent_loader.DataLoader()
    monkeypatch.setattr(
        tencent_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )

    def boom(code, start, end):
        calls.append(code)
        raise RuntimeError("HTTP Error 501: Not Implemented")

    monkeypatch.setattr(loader, "_fetch_one", boom)
    loader.fetch(
        ["000001.SZ", "000002.SZ", "000004.SZ"],
        "2026-01-01",
        "2026-01-31",
        interval="1D",
    )
    assert calls == ["000001.SZ"]
