"""Unit tests for the capital-flow (ETF-OI) model.

The financial maths is tested against a hand-built two-fund series so the
expected AUM / flow / ratio numbers can be verified by hand; the network layer
is never touched.
"""

from __future__ import annotations

import pytest

from src.capital import sources
from src.capital.etf_oi import (
    _percentile_rank,
    _regime,
    _score,
    _streak,
    _window_sum,
    _zscore,
    build_series,
    compute_stats,
)
from src.capital.models import CapitalPoint, EtfOiReport, FlowStats
from src.capital.universe import CSI300_FUNDS, exchange_of, universe

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07"]
INDEX = {DAYS[0]: 4000.0, DAYS[1]: 4050.0, DAYS[2]: 4000.0}
MCAP = {
    "as_of": "2026-01-07",
    "weights_date": "2025-12-31",
    "constituents": 300,
    "covered": 300,
    # 50 万亿 of float at index 4000 → 12.5 亿 per index point.
    "total_circ_mv": 5.0e13,
    "total_weight_implied": 4.5e13,
}


def _patch_sources(monkeypatch, sse, szse, navs):
    monkeypatch.setattr(sources, "index_close_history", lambda *a, **k: dict(INDEX))
    monkeypatch.setattr(sources, "prefetch_sse_shares", lambda days, workers=6: {d: sse.get(d, {}) for d in days})
    monkeypatch.setattr(sources, "szse_etf_shares", lambda start, end: {d: szse.get(d, {}) for d in DAYS})
    monkeypatch.setattr(sources, "prefetch_nav", lambda codes, workers=6: {c: navs.get(c, {}) for c in codes})
    monkeypatch.setattr(sources, "csi300_market_cap", lambda close: dict(MCAP))


@pytest.fixture()
def two_fund_series(monkeypatch):
    """510300 (SH) doubles its share growth; 159919 (SZ) only marks to market."""
    sse = {
        DAYS[0]: {"510300": 100e8},
        DAYS[1]: {"510300": 110e8},
        DAYS[2]: {"510300": 110e8},
    }
    szse = {
        DAYS[0]: {"159919": 50e8},
        DAYS[1]: {"159919": 50e8},
        DAYS[2]: {"159919": 50e8},
    }
    navs = {
        "510300": {DAYS[0]: 4.0, DAYS[1]: 4.4, DAYS[2]: 4.0},
        "159919": {DAYS[0]: 1.5, DAYS[1]: 1.65, DAYS[2]: 1.5},
    }
    _patch_sources(monkeypatch, sse, szse, navs)
    return build_series(window=20, codes="510300,159919")


def test_universe_defaults_and_override():
    assert len(CSI300_FUNDS) >= 10
    codes = [f.code for f in universe()]
    assert "510300" in codes and "159919" in codes

    override = universe("510300, 159919 510300 999999")
    assert [f.code for f in override] == ["510300", "159919", "999999"]
    # Unknown codes keep the exchange suffix convention instead of guessing a name.
    assert override[2].exchange == "SH"
    assert exchange_of("159919") == "SZ"
    assert exchange_of("588000") == "SH"
    assert exchange_of("600000") == "SH"


def test_build_series_stock_and_flow(two_fund_series):
    report = two_fund_series
    assert isinstance(report, EtfOiReport)
    assert [p.date for p in report.points] == DAYS

    d1, d2, d3 = report.points

    # Day 1 — no prior day, so no flow can be asserted.
    assert d1.aum == pytest.approx(475e8)
    assert d1.net_flow is None
    assert d1.oi_pct == pytest.approx(475e8 / 5.0e13 * 100)

    # Day 2 — 10 亿 new shares on 510300 priced at the previous NAV of 4.0.
    assert d2.aum == pytest.approx(566.5e8)
    assert d2.net_flow == pytest.approx(40e8)
    # Market cap scales with the index: 5.0e13 × 4050/4000.
    assert d2.index_mcap == pytest.approx(5.0e13 * 4050 / 4000)
    assert d2.oi_pct == pytest.approx(566.5e8 / (5.0e13 * 4050 / 4000) * 100)
    # NAV move on the carried shares is P&L, not a subscription.
    assert d2.nav_pnl == pytest.approx(100e8 * 0.4 + 50e8 * 0.15)

    # Day 3 — flat shares, NAV gives back the gain.
    assert d3.net_flow == pytest.approx(0.0)
    assert d3.nav_pnl == pytest.approx(-51.5e8)
    assert d3.aum == pytest.approx(515e8)

    assert report.stats.oi_pct == pytest.approx(d3.oi_pct)
    assert report.stats.flow_window == pytest.approx(40e8)
    assert report.provisional is False
    assert report.universe_size == 2
    # Detail table: biggest fund first, group weights add to one.
    assert [f.code for f in report.funds] == ["510300", "159919"]
    assert sum(f.group_weight or 0 for f in report.funds) == pytest.approx(1.0)
    assert report.funds[0].net_flow == pytest.approx(0.0)


def test_missing_shares_mark_day_provisional(monkeypatch):
    """A bourse that has not published yet must not fake a redemption."""
    sse = {DAYS[0]: {"510300": 100e8}, DAYS[1]: {"510300": 110e8}, DAYS[2]: {"510300": 110e8}}
    szse = {DAYS[0]: {"159919": 50e8}, DAYS[1]: {"159919": 50e8}}  # day 3 missing
    navs = {
        "510300": {DAYS[0]: 4.0, DAYS[1]: 4.4, DAYS[2]: 4.0},
        "159919": {DAYS[0]: 1.5, DAYS[1]: 1.65, DAYS[2]: 1.5},
    }
    _patch_sources(monkeypatch, sse, szse, navs)
    report = build_series(window=20, codes="510300,159919")

    last = report.points[-1]
    assert last.provisional is True
    assert last.missing_count == 1
    assert last.net_flow is None
    # AUM carries the last observed shares instead of dropping the fund.
    assert last.aum == pytest.approx(440e8 + 75e8)
    assert report.provisional is True
    assert any("待披露" in w or "未披露" in w for w in report.warnings)


def test_stats_helpers():
    assert _window_sum([1.0, 2.0, 3.0], 2, 2) == pytest.approx(5.0)
    assert _window_sum([1.0], 5, 3) is None

    assert _streak([1.0, 2.0, 3.0]) == 3
    assert _streak([-1.0, -2.0]) == -2
    assert _streak([1.0, -1.0]) == -1
    # Trailing unpublished days are skipped, not treated as a break.
    assert _streak([1.0, 2.0, None]) == 2

    assert _percentile_rank(3.0, [1.0, 2.0, 3.0]) == pytest.approx(100.0 * 2.5 / 3.0)
    assert _zscore(2.0, [1.0] * 19) is None  # too few observations
    assert _zscore(2.0, [1.0] * 25) is None  # zero variance
    z = _zscore(3.0, [1.0, 2.0, 3.0] * 7)
    assert z is not None and z > 0

    assert _regime(80) == "surge"
    assert _regime(60) == "inflow"
    assert _regime(50) == "neutral"
    assert _regime(30) == "outflow"
    assert _regime(10) == "drain"
    assert _regime(None) == "neutral"

    strong = _score(FlowStats(flow_z=2.0, flow_20d_bp=60.0, inflow_streak=4))
    weak = _score(FlowStats(flow_z=-2.0, flow_20d_bp=-60.0, inflow_streak=-4))
    assert strong is not None and weak is not None
    assert strong > 60 > 40 > weak
    assert _score(FlowStats()) is None


def test_compute_stats_basic_series():
    points = []
    for i, day in enumerate(DAYS):
        points.append(
            CapitalPoint(
                date=day,
                aum=500e8 + i * 10e8,
                index_close=4000.0,
                index_mcap=5.0e13,
                oi_pct=0.1 + i * 0.002,
                net_flow=10e8,
                flow_bp=0.2,
            )
        )
    stats = compute_stats(points)
    assert stats.observed_days == 3
    assert stats.oi_change_bp == pytest.approx(0.2)
    assert stats.aum_change == pytest.approx(10e8)
    assert stats.flow_5d == pytest.approx(30e8)
    assert stats.flow_20d is None  # needs 10 observations
    assert stats.inflow_streak == 3
    assert stats.oi_percentile == pytest.approx(100.0 * 2.5 / 3.0)


def test_report_roundtrip():
    report = EtfOiReport(
        as_of="2026-01-07",
        window=120,
        points=[CapitalPoint(date="2026-01-07", aum=1.0, oi_pct=0.5)],
        stats=FlowStats(oi_pct=0.5, regime="inflow"),
    )
    restored = EtfOiReport.from_dict(report.to_dict())
    assert restored.as_of == "2026-01-07"
    assert restored.points[0].aum == pytest.approx(1.0)
    assert restored.stats.regime == "inflow"
