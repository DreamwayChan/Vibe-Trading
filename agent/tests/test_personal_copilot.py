"""Unit and API tests for P1-P3 personal trading copilot modules:
  - Market Breadth thermometer (warm / neutral / cold + ProcessMandate tightening)
  - 5-Dimension Conviction Funnel (Top 2-3 focus cap + safety vetoes)
  - Event Risk filter (earnings window & lockup unlock vetoes)
  - My Real Portfolio store + Next-Session Trade Plan (risk-per-trade sizing, -10% stop cap)
  - Pre-Trade AI Copilot (advisory-only boundary, LLM merge guard, 5-field journal draft)
  - 16:30 Daily Close Routine + FastAPI /monitor/* routes
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api_server
from src.daily_routine import run_daily_close_routine
from src.data.breadth import compute_breadth_from_map
from src.data.event_risk import assess_event_risks
from src.monitor.advisory import generate_symbol_advisory
from src.monitor.conviction import build_conviction_summary
from src.monitor.models import HoldingRow, MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.my_portfolio import MyPortfolio, RealPosition, load_my_portfolio, normalize_ashare_symbol, save_my_portfolio
from src.monitor.store import save_monitor
from src.monitor.trade_plan import build_trade_plan
from src.process.compose import compose


def _make_ohlcv(closes: list[float], *, start: str = "2026-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    arr = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": arr * 0.995,
            "high": arr * 1.015,
            "low": arr * 0.985,
            "close": arr,
            "volume": 100_000.0,
        },
        index=idx,
    )


def test_breadth_thermometer_classifies_warm_and_cold_and_tightens_process() -> None:
    # 35 rising stocks -> warm breadth
    warm_map = {
        f"{600000 + i:06d}.SH": _make_ohlcv([10.0 + 0.1 * d for d in range(25)])
        for i in range(35)
    }
    as_of = warm_map["600000.SH"].index[-1].date().isoformat()
    warm = compute_breadth_from_map(warm_map, as_of=as_of)
    assert warm.sufficient_sample is True
    assert warm.state == "warm"
    assert warm.stance_cap == "full"
    assert (warm.adv_ratio or 0) > 0.9

    # 35 limit-down crashing stocks -> cold breadth -> ProcessMandate blocks new buys!
    cold_map = {
        f"{600100 + i:06d}.SH": _make_ohlcv([20.0] * 24 + [18.0])  # -10% limit down
        for i in range(35)
    }
    cold = compute_breadth_from_map(cold_map, as_of=as_of)
    assert cold.sufficient_sample is True
    assert cold.state == "cold"
    assert cold.stance_cap == "none"

    dec = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=[HoldingRow(symbol="600100.SH", weight=0.3, action="enter")],
        as_of=as_of,
        breadth=cold.to_dict(),
    )
    assert "mandate:breadth_cold" in dec.vetoed_by
    assert dec.per_symbol[0].blocked is True
    assert dec.per_symbol[0].weight == 0.0


def test_conviction_funnel_caps_focus_to_max_3_and_vetoes_earnings_landmine() -> None:
    idx_closes = [10.0 + 0.05 * i for i in range(30)]
    data_map = {
        f"00000{i}.SZ": _make_ohlcv(idx_closes)
        for i in range(1, 7)
    }
    as_of = data_map["000001.SZ"].index[-1].date().isoformat()

    cfg = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_conv"),
        holdings=[
            HoldingRow(symbol=f"00000{i}.SZ", weight=1 / 6, action="enter", price=idx_closes[-1])
            for i in range(1, 7)
        ],
        suggestions=[
            MonitorSuggestion(symbol=f"00000{i}.SZ", action="buy", reason="s", as_of=as_of, price=idx_closes[-1], tradable=True)
            for i in range(1, 7)
        ],
        validation_gate={"passed": True, "reasons": []},
        tradable=True,
    )
    # 000001.SZ has an earnings disclosure tomorrow -> hard veto!
    ev_flags = assess_event_risks(
        [f"00000{i}.SZ" for i in range(1, 7)],
        as_of=as_of,
        events=[{"symbol": "000001.SZ", "type": "earnings", "date": as_of}],
    )
    sector_ranks = {
        f"00000{i}.SZ": ("算力硬件", i, 80)
        for i in range(1, 7)
    }
    conv = build_conviction_summary(
        cfg,
        as_of=as_of,
        data_map=data_map,
        sector_ranks=sector_ranks,
        event_flags_by_symbol=ev_flags,
        max_focus=3,
    )
    assert len(conv.focus_symbols) == 3
    assert "000001.SZ" not in conv.focus_symbols
    assert conv.focus_symbols == ["000002.SZ", "000003.SZ", "000004.SZ"]
    item_1 = next(x for x in conv.items if x.symbol == "000001.SZ")
    assert item_1.vetoed is True
    assert item_1.tier == "avoid"


def test_my_portfolio_and_trade_plan_risk_sizing_and_stop_discipline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert normalize_ashare_symbol("600519") == "600519.SH"
    assert normalize_ashare_symbol("300750") == "300750.SZ"
    assert normalize_ashare_symbol("830799") == "830799.BJ"

    portfolio = save_my_portfolio(
        MyPortfolio(
            total_capital=200_000.0,
            max_positions=3,
            max_stop_loss_pct=0.10,
            risk_per_trade_pct=0.015,  # max ¥3,000 loss per trade
            positions=[
                # Held position down -12% from cost 25.0 -> triggers -10% stop_exit!
                RealPosition(symbol="000009.SZ", shares=1000, cost_price=25.0, buy_date="2026-01-02"),
                # Healthy held position -> kept (uses 1 of 3 slots -> 2 slots left!)
                RealPosition(symbol="000008.SZ", shares=1000, cost_price=20.0, buy_date="2026-01-02"),
            ],
        )
    )
    loaded = load_my_portfolio()
    assert len(loaded.positions) == 2

    closes_up = [30.0 + 0.1 * i for i in range(25)]
    as_of = "2026-02-04"
    data_map = {
        "000009.SZ": _make_ohlcv([25.0] * 24 + [22.0]),  # -12% vs 25.0 cost
        "000008.SZ": _make_ohlcv([20.0] * 24 + [21.5]),  # +7.5% healthy
        "300750.SZ": _make_ohlcv(closes_up),
    }
    cfg = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_plan"),
        holdings=[
            HoldingRow(symbol="000008.SZ", weight=0.33, action="hold", price=21.5),
            HoldingRow(symbol="300750.SZ", weight=0.33, action="enter", price=closes_up[-1]),
        ],
        suggestions=[
            MonitorSuggestion(symbol="300750.SZ", action="buy", reason="s", as_of=as_of, exec_as_of="2026-02-05", price=closes_up[-1], tradable=True)
        ],
        validation_gate={"passed": True, "reasons": []},
        tradable=True,
    )
    conv = build_conviction_summary(
        cfg,
        as_of=as_of,
        data_map=data_map,
        sector_ranks={"300750.SZ": ("电池", 2, 80), "000008.SZ": ("轨交", 10, 80)},
        max_focus=3,
    )
    plan = build_trade_plan(
        cfg,
        as_of=as_of,
        exec_session="2026-02-05",
        conviction=conv,
        portfolio=portfolio,
        data_map=data_map,
    )
    pos_by_sym = {r.symbol: r for r in plan.position_actions}
    assert pos_by_sym["000009.SZ"].action == "stop_exit"
    assert pos_by_sym["000009.SZ"].planned_shares == 1000
    assert pos_by_sym["000008.SZ"].action == "hold_keep"
    assert plan.slots_available == 2

    assert len(plan.entry_plans) == 1
    entry = plan.entry_plans[0]
    assert entry.symbol == "300750.SZ"
    assert entry.action == "buy_focus"
    assert entry.planned_shares >= 100 and entry.planned_shares % 100 == 0
    # Stop distance never exceeds habitual 10%, and max RMB loss stays within 1.5% budget!
    assert (entry.stop_distance_pct or 0) <= 0.1001
    assert (entry.max_loss_rmb or 0) <= 200_000.0 * 0.015 + 1e-3
    assert (entry.chase_stop_above or 0) > entry.ref_close


def test_advisory_copilot_never_mutates_tradable_and_builds_valid_five_field_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id="run_adv"),
        holdings=[HoldingRow(symbol="600519.SH", weight=0.25, action="enter", price=1500.0)],
        suggestions=[
            MonitorSuggestion(
                symbol="600519.SH",
                action="buy",
                reason="s",
                as_of="2026-02-04",
                exec_as_of="2026-02-05",
                price=1500.0,
                tradable=False,
                block_reasons=["missing_journal"],
            )
        ],
        validation_gate={"passed": True, "reasons": []},
        tradable=False,
        tradable_reasons=["missing_journal"],
    )

    # Simulate an overly optimistic LLM trying to say "execute_plan" on a wait/probe setup:
    fake_llm = lambda sys_p, usr_p: json.dumps(
        {
            "verdict": "execute_plan",
            "verdict_label": "可重点关注低吸",
            "supporting_points": ["主线顺风", "回踩MA20支撑"],
            "devil_advocate_risks": ["尚未填写五栏交易日志，需先补齐失效线"],
            "tomorrow_playbook": ["平开低吸，高开超2.5%放弃"],
            "narrative": "结构健康但需先完成日志门禁。",
        }
    )
    report = generate_symbol_advisory(
        cfg,
        "600519",
        user_note="明天高开能追吗？",
        use_llm=True,
        llm_invoker=fake_llm,
    )
    assert report.advisory_only is True
    assert report.mode == "llm"
    assert cfg.tradable is False  # Board state strictly untouched!
    # Check 5-field journal draft passes DecisionJournal field validation!
    from src.hypotheses.journal import assess_journal_fields

    draft = report.journal_draft
    reasons = assess_journal_fields(
        hypothesis_id="",
        thesis=draft["thesis"],
        invalidation=draft["invalidation"],
        planned_position=draft["planned_position"],
        planned_risk=draft["planned_risk"],
        timestamp="2026-02-04T15:30:00Z",
        hypothesis_found=False,
        observation=draft["observation"],
        hypothesis=draft["hypothesis"],
        logic=draft["logic"],
        validation_plan=draft["validation_plan"],
    )
    assert reasons == []


def test_monitor_copilot_http_routes_and_daily_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")

    run_id = "20260201_000000_00_routetest"
    run_dir = tmp_path / "runs" / run_id
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, data_map):",
                "        return {c: pd.Series(0.5, index=df.index) for c, df in data_map.items()}",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(
        json.dumps({"source": "auto", "interval": "1D", "codes": ["000001.SZ", "000002.SZ"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.api.helpers.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("src.monitor.scan.RUNS_DIR", tmp_path / "runs")

    frame = _make_ohlcv([12.0 + 0.1 * i for i in range(25)])
    monkeypatch.setattr(
        "src.monitor.scan._fetch_universe_ohlcv",
        lambda universe, **kw: ({s: frame.copy() for s in universe}, {}),
    )
    monkeypatch.setattr(
        "src.monitor.scan._apply_same_day_overlay",
        lambda dm, **kw: (dm, {k: False for k in dm}, {}),
    )

    save_monitor(
        MonitorConfig(
            symbols=["000001.SZ"],
            strategy=StrategyRef(type="run_dir", run_id=run_id),
        )
    )

    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    # 1. Save personal portfolio via PUT /monitor/my-portfolio
    put_res = client.put(
        "/monitor/my-portfolio",
        json={
            "total_capital": 150_000.0,
            "max_positions": 2,
            "max_stop_loss_pct": 0.10,
            "risk_per_trade_pct": 0.015,
            "positions": [{"symbol": "000001", "shares": 1000, "cost_price": 13.5}],
        },
    )
    assert put_res.status_code == 200, put_res.text
    assert put_res.json()["portfolio"]["positions"][0]["symbol"] == "000001.SZ"

    # 2. Run 16:30 daily-close routine via POST /monitor/daily-close
    close_res = client.post(
        "/monitor/daily-close",
        json={"as_of": "2026-02-04", "settle_paper_book": False},
    )
    assert close_res.status_code == 200, close_res.text
    payload = close_res.json()
    assert "次日交易计划" in payload["routine"]["digest_markdown"]
    assert payload["board"]["conviction"] is not None
    assert payload["board"]["trade_plan"] is not None

    # 3. Call Pre-Trade AI Copilot (deterministic offline mode) via POST /monitor/advisory
    adv_res = client.post(
        "/monitor/advisory",
        json={"symbol": "000002.SZ", "user_note": "盈亏比合适吗？", "use_llm": False},
    )
    assert adv_res.status_code == 200, adv_res.text
    adv = adv_res.json()
    assert adv["advisory_only"] is True
    assert adv["symbol"] == "000002.SZ"
    assert adv["journal_draft"]["symbol"] == "000002.SZ"
