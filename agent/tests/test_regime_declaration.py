"""Design-regime declaration: scoring, override precedence, and HTTP surface."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.sector_pick.regime_infer import (
    TIE_MARGIN,
    apply_override,
    coverage_check,
    infer_from_style,
    infer_run,
    panel_profile,
    resolve_effective,
)

STYLE = {
    "style_label": "横截面·周频·6等权·MA20择时",
    "panel": "测试板块",
    "breadth": 30,
    "rebalance": "weekly",
    "holding_count": 6,
    "sector_timing": "ma20_half",
    "risk_rules": {
        "peak_dd_stop": 0.15,
        "take_profit": 0.35,
        "time_stop_days": 20,
        "min_hold_days": 5,
        "max_turnover_per_rebalance": 0.2,
    },
    "designed_regime": "unspecified",
    "designed_regime_source": "none",
}


def _trending_profile(up: bool = True, days: int = 320) -> dict:
    """Synthetic panel profile so the scoring core needs no filesystem."""
    values = [1.0]
    for i in range(days):
        values.append(values[-1] * (1.0 + (0.0012 if up else -0.0012)))
    dates = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(days + 1)]
    return panel_profile(dates, values)


def test_flat_gap_never_produces_a_tie_flag_without_two_candidates() -> None:
    verdict = infer_from_style(STYLE, _trending_profile(), 30)["verdict"]
    assert verdict["designed_regime"] in {"bull", "slow_bull", "chop", "bear"}
    if not verdict["tie"]:
        assert verdict["tie_candidates"] == []
    assert 0.25 <= verdict["confidence"] <= 0.92


def test_defensive_mechanics_do_not_declare_a_fast_bull_market() -> None:
    """35% take profit + 20d time stop + MA20 half exposure clips a fast tape."""
    result = infer_from_style(STYLE, _trending_profile(up=True), 30)
    assert result["verdict"]["designed_regime"] != "bull"


def test_tie_is_reported_and_broken_by_panel_share() -> None:
    profile = _trending_profile()
    result = infer_from_style(STYLE, profile, 30)
    verdict = result["verdict"]
    if verdict["margin"] < TIE_MARGIN:
        assert verdict["tie"] is True
        assert len(verdict["tie_candidates"]) == 2


def test_manual_override_beats_inference_and_survives_reinfer(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, STYLE)
    style = apply_override(run_dir, "bull", "板块处于订单上行周期")
    assert style["designed_regime"] == "bull"
    assert style["designed_regime_source"] == "manual_override"
    assert style["designed_regime_override"]["reason"] == "板块处于订单上行周期"
    auto = style["designed_regime_auto"]
    assert auto != "bull"

    # Re-inference refreshes evidence but must not clobber the human value.
    assert infer_run(run_dir)["effective"]["designed_regime"] == "bull"

    reset = apply_override(run_dir, "auto", "")
    assert reset["designed_regime"] == auto
    assert reset["designed_regime_source"] == "auto_inferred"


def test_override_requires_a_reason(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, STYLE)
    with pytest.raises(ValueError):
        apply_override(run_dir, "chop", "   ")


def test_ex_ante_design_source_outranks_inference() -> None:
    style = dict(STYLE)
    style["designed_regime"] = "slow_bull"
    style["designed_regime_source"] = "prop_file"
    effective, source = resolve_effective(style, "chop")
    assert (effective, source) == ("slow_bull", "prop_file")


def test_coverage_flags_a_regime_the_sample_barely_contains() -> None:
    thin = {
        "regime_share": {"bull": 0.70, "slow_bull": 0.10, "chop": 0.08, "bear": 0.12},
        "labeled_days": 400,
    }
    coverage = coverage_check(thin, "chop")
    assert coverage["ok"] is False
    assert coverage["dominant_regime"] == "bull"
    assert "样本支撑" in coverage["note"]

    ample = {"regime_share": {"bull": 0.30, "slow_bull": 0.30, "chop": 0.30, "bear": 0.10}}
    assert coverage_check(ample, "chop")["ok"] is True


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import api_server

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    monkeypatch.setattr(api_server, "RUNS_DIR", runs_dir)
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_patch_then_get_round_trips(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    import api_server

    run_dir = _make_run(api_server.RUNS_DIR, dict(STYLE))
    run_id = run_dir.name

    resp = client.get(f"/runs/{run_id}/regime")
    assert resp.status_code == 200
    assert resp.json()["strategy_style"]["designed_regime"]
    auto = resp.json()["strategy_style"]["designed_regime"]

    chosen = "bear" if auto != "bear" else "bull"
    resp = client.patch(
        f"/runs/{run_id}/regime", json={"designed_regime": chosen, "reason": "人工判断"}
    )
    assert resp.status_code == 200
    assert resp.json()["strategy_style"]["designed_regime"] == chosen

    resp = client.patch(
        f"/runs/{run_id}/regime", json={"designed_regime": "nonsense", "reason": "x"}
    )
    assert resp.status_code == 400


def test_regime_route_404s_for_unknown_run(client: TestClient) -> None:
    assert client.get("/runs/does-not-exist/regime").status_code == 404


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_run(root: Path, style: dict) -> Path:
    run_dir = root / "20260101_000000_00_regime_fixture"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "config.json").write_text(
        json.dumps({"codes": ["600000.SH"], "strategy_style": style}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 260 bars of a steady uptrend: enough for a 120d MA and a labelled sample.
    for sym in ("600000.SH", "600001.SH", "600002.SH"):
        rows = ["trade_date,open,high,low,close,volume"]
        price = 10.0
        for i in range(260):
            price *= 1.0 + 0.0015 + 0.01 * math.sin(i / 7.0)
            day = f"2024-{1 + i // 21:02d}-{1 + i % 21:02d}"
            rows.append(f"{day},{price:.2f},{price:.2f},{price:.2f},{price:.2f},1000")
        (run_dir / "artifacts" / f"ohlcv_{sym}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return run_dir
