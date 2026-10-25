"""Compose waterfall: later gates only tighten; analog never opens a buy."""

from __future__ import annotations

from src.monitor.models import HoldingRow
from src.process.attach import clip_holdings, decision_after_forecast, decision_for_scan
from src.process.compose import compose
from src.process.mandate import DEFAULT_MANDATE, ProcessMandate
from src.process.models import AnalogCall, Decision


def _row(
    symbol: str,
    *,
    weight: float,
    action: str = "enter",
    structure: str | None = "sell_rally",
    prev_weight: float | None = None,
) -> HoldingRow:
    trend = None
    if structure:
        trend = {
            "market_regime": "chop",
            "stock_regime": "chop",
            "swing": "overbought",
            "action": structure,
            "fade_rallies": True,
            "reason": "fixture",
        }
    return HoldingRow(
        symbol=symbol,
        weight=weight,
        action=action,  # type: ignore[arg-type]
        prev_weight=prev_weight,
        trend=trend,
    )


def test_conflict_day_f0_full_structure_fade_does_not_enter() -> None:
    """F0 full is eligibility, not a buy order, when the book is all fade."""
    holdings = [
        _row("AAA.SZ", weight=0.16, action="enter", structure="sell_rally"),
        _row("BBB.SZ", weight=0.16, action="enter", structure="sell_rally"),
        _row("CCC.SZ", weight=0.16, action="enter", structure="reduce"),
    ]
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.action in ("wait", "hold", "trim")
    assert decision.action not in ("enter", "add")
    assert decision.size_nav == 0.0 or all(
        abs(row.weight) < 1e-9 or row.action != "enter" for row in decision.per_symbol
    )
    assert any(row.blocked for row in decision.per_symbol)
    assert "structure:fade" in decision.vetoed_by or any(
        "structure" in (row.block_reason or "") for row in decision.per_symbol
    )
    clipped = clip_holdings(holdings, decision)
    assert all(row.action != "enter" for row in clipped)


def test_conflict_mixed_book_keeps_rs_hold_blocks_fade_enter() -> None:
    holdings = [
        _row("RS.SZ", weight=0.20, action="hold", structure="hold", prev_weight=0.20),
        _row("FADE.SZ", weight=0.16, action="enter", structure="sell_rally"),
    ]
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    by_symbol = {row.symbol: row for row in decision.per_symbol}
    assert by_symbol["RS.SZ"].action == "hold"
    assert by_symbol["RS.SZ"].weight > 0
    assert by_symbol["FADE.SZ"].action != "enter"
    assert by_symbol["FADE.SZ"].weight == 0.0
    assert decision.action in ("hold", "trim")
    assert decision.action != "enter"


def test_aligned_chop_buy_dip_may_enter() -> None:
    holdings = [
        _row("DIP.SZ", weight=0.16, action="enter", structure="buy_dip"),
        _row("RS.SZ", weight=0.16, action="enter", structure="hold"),
        _row("RS2.SZ", weight=0.16, action="hold", structure="hold", prev_weight=0.16),
    ]
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.action in ("enter", "hold", "add")
    assert decision.size_nav > 0
    entered = [row for row in decision.per_symbol if row.action == "enter"]
    assert entered


def test_index_bear_is_wait_size_zero() -> None:
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="hold")]
    decision = compose(
        market_regime="bear",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.action == "wait"
    assert decision.size_nav == 0.0
    assert "mandate:bear" in decision.vetoed_by


def test_medium_bear_long_chop_keeps_existing_hold() -> None:
    """Year-line still intact: no new longs, but do not flatten a live book."""
    holdings = [_row("AAA.SZ", weight=0.2, action="hold", structure="hold", prev_weight=0.2)]
    decision = compose(
        market_regime="bear",
        f0_stance="half_watch",
        holdings=holdings,
        horizons={"short": "bear", "medium": "bear", "long": "chop"},
        as_of="2024-06-03",
    )
    assert decision.size_nav > 0
    assert decision.action not in ("enter", "add")
    assert "mandate:bear" in decision.vetoed_by


def test_index_bull_does_not_add() -> None:
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="hold")]
    decision = compose(
        market_regime="bull",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.action not in ("enter", "add")
    assert "structure:index_bull" in decision.vetoed_by


def test_provisional_bar_blocks_new_risk() -> None:
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="buy_dip")]
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        provisional=True,
        as_of="2024-06-03",
    )
    assert decision.action != "enter"
    assert all(row.action != "enter" for row in decision.per_symbol)
    assert "mandate:provisional" in decision.vetoed_by


def test_analog_none_does_not_loosen() -> None:
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="sell_rally")]
    without = compose(market_regime="chop", f0_stance="full", holdings=holdings)
    with_empty = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        analog={},
    )
    assert without.action == with_empty.action
    assert without.size_nav == with_empty.size_nav
    assert without.analog_applied is False
    assert with_empty.analog_applied is False


def test_analog_down_call_blocks_enter_does_not_create_buy() -> None:
    holdings = [
        _row("DOWN.SZ", weight=0.16, action="enter", structure="buy_dip"),
        _row("OK.SZ", weight=0.16, action="enter", structure="buy_dip"),
    ]
    analog = {
        "DOWN.SZ": AnalogCall(symbol="DOWN.SZ", bias="down", called=True, confidence=0.4),
        "OK.SZ": AnalogCall(symbol="OK.SZ", bias="up", called=True, confidence=0.4),
    }
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        analog=analog,
        as_of="2024-06-03",
    )
    by_symbol = {row.symbol: row for row in decision.per_symbol}
    assert by_symbol["DOWN.SZ"].weight == 0.0
    assert by_symbol["DOWN.SZ"].action != "enter"
    # Up analog must not be the reason OK.SZ enters — F0+structure already allow it.
    assert by_symbol["OK.SZ"].action == "enter"
    assert decision.analog_applied is True
    assert any(step.gate == "analog" and not step.skipped for step in decision.trace)


def test_analog_recompose_cannot_reopen_wait() -> None:
    wait = Decision(action="wait", size_nav=0.0, reason="wait", as_of="2024-06-03")
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="buy_dip")]
    analog = {"AAA.SZ": AnalogCall(symbol="AAA.SZ", bias="up", called=True)}
    second = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        analog=analog,
        previous=wait,
        as_of="2024-06-03",
    )
    assert second.action == "wait"
    assert second.size_nav == 0.0


def test_analog_recompose_cannot_grow_size() -> None:
    previous = compose(
        market_regime="chop",
        f0_stance="half_watch",
        holdings=[_row("AAA.SZ", weight=0.30, action="hold", structure="hold", prev_weight=0.30)],
        as_of="2024-06-03",
    )
    # A looser second pass (full + buy_dip enter) must still cap at previous size.
    second = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=[_row("AAA.SZ", weight=0.30, action="enter", structure="buy_dip", prev_weight=0.30)],
        analog={"AAA.SZ": AnalogCall(symbol="AAA.SZ", bias="up", called=True)},
        previous=previous,
        as_of="2024-06-03",
    )
    assert second.size_nav <= previous.size_nav + 1e-9


def test_half_watch_is_not_full_buy() -> None:
    holdings = [_row("AAA.SZ", weight=0.2, action="enter", structure="buy_dip")]
    decision = compose(
        market_regime="chop",
        f0_stance="half_watch",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.action != "enter"
    assert decision.size_nav <= DEFAULT_MANDATE.book_nav_cap * DEFAULT_MANDATE.half_watch_mult + 1e-9


def test_size_never_scales_above_engine_or_cap() -> None:
    holdings = [
        _row("AAA.SZ", weight=0.30, action="hold", structure="hold", prev_weight=0.30),
        _row("BBB.SZ", weight=0.30, action="hold", structure="hold", prev_weight=0.30),
    ]
    decision = compose(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        as_of="2024-06-03",
    )
    assert decision.size_nav <= DEFAULT_MANDATE.book_nav_cap + 1e-9
    assert decision.size_nav <= 0.60 + 1e-9


def test_decision_roundtrip_dict() -> None:
    holdings = [_row("AAA.SZ", weight=0.1, action="hold", structure="hold", prev_weight=0.1)]
    decision = compose(market_regime="chop", f0_stance="full", holdings=holdings)
    restored = Decision.from_dict(decision.to_dict())
    assert restored is not None
    assert restored.action == decision.action
    assert restored.size_nav == decision.size_nav
    assert len(restored.trace) == len(decision.trace)
    assert restored.mandate_hash == DEFAULT_MANDATE.fingerprint()


def test_daily_scan_strips_analog_unless_flag_on() -> None:
    holdings = [_row("DOWN.SZ", weight=0.16, action="enter", structure="buy_dip")]
    analog = {"DOWN.SZ": AnalogCall(symbol="DOWN.SZ", bias="down", called=True)}
    daily = decision_for_scan(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        provisional=False,
        as_of="2024-06-03",
        analog=analog,
    )
    assert daily.analog_applied is False
    assert daily.action == "enter"

    forecast = decision_after_forecast(
        market_regime="chop",
        f0_stance="full",
        holdings=holdings,
        provisional=False,
        as_of="2024-06-03",
        forecast_payload={
            "items": [
                {
                    "symbol": "DOWN.SZ",
                    "assessment": {"called": True, "bias": "down", "confidence": 0.5},
                }
            ]
        },
        previous=daily,
    )
    assert forecast.size_nav <= daily.size_nav + 1e-9
    by_symbol = {row.symbol: row for row in forecast.per_symbol}
    assert by_symbol["DOWN.SZ"].weight == 0.0


def test_mandate_fingerprint_changes_with_knobs() -> None:
    other = ProcessMandate(book_nav_cap=0.25)
    assert other.fingerprint() != DEFAULT_MANDATE.fingerprint()
