"""Attach a composed Decision onto scan / forecast payloads."""

from __future__ import annotations

from typing import Any, Mapping

from src.monitor.models import WEIGHT_EPSILON, HoldingRow
from src.process.analog import analog_calls_from_forecast
from src.process.compose import apply_decision_to_holdings, compose
from src.process.mandate import DEFAULT_MANDATE, ProcessMandate
from src.process.models import Decision


def implied_book_stance(
    holdings: list[HoldingRow] | None,
    market_regime: str | None,
    horizons: Mapping[str, Any] | None = None,
) -> str:
    """F0-like eligibility when the caller is a single bound strategy, not a catalog.

    A non-empty book is treated as ``full``, then the same index cap as sector-pick:
    bear → none (flatten) unless the year-line is still not bear, in which case
    an existing book is ``half_watch`` (hold, do not add). Bull → half_watch.
    """
    has_book = any(
        abs(float(getattr(row, "weight", 0.0) or 0.0)) > WEIGHT_EPSILON
        for row in (holdings or [])
    )
    base = "full" if has_book else "none"
    regime = (market_regime or "").strip().lower() or None
    if regime == "bear":
        long_regime = None
        if isinstance(horizons, Mapping):
            value = str(horizons.get("long") or "").strip().lower()
            if value in ("bull", "chop", "bear"):
                long_regime = value
        if has_book and long_regime not in (None, "bear"):
            return "half_watch"
        return "none"
    if regime == "bull" and base == "full":
        return "half_watch"
    return base


def market_regime_from_holdings(holdings: list[HoldingRow] | None) -> str | None:
    """Read CSI 300 regime off the first stamped trend plan."""
    for row in holdings or []:
        trend = getattr(row, "trend", None)
        if isinstance(trend, dict):
            regime = str(trend.get("market_regime") or "").strip().lower()
            if regime in ("bull", "chop", "bear"):
                return regime
    return None


def decision_for_scan(
    *,
    market_regime: str | None,
    f0_stance: str,
    holdings: list[HoldingRow] | None,
    provisional: bool,
    as_of: str | None,
    overlays_reason: str | None = None,
    analog: Mapping[str, Any] | None = None,
    previous: Decision | None = None,
    mandate: ProcessMandate | None = None,
    extra_overlays: Mapping[str, Any] | None = None,
    horizons: Mapping[str, Any] | None = None,
) -> Decision:
    """Daily-scan compose. Analog is omitted unless the caller passes calls."""
    rules = mandate or DEFAULT_MANDATE
    analog_map = analog if analog else None
    if analog_map and not rules.analog_on_daily_scan and previous is None:
        # First pass (the scan) ignores analog even if someone stuffed calls in.
        analog_map = None
    overlays: dict[str, Any] = {"reason": overlays_reason or "", "f0_stance": f0_stance}
    if extra_overlays:
        overlays.update(dict(extra_overlays))
    if horizons:
        overlays.setdefault("horizons", dict(horizons))
    return compose(
        market_regime=market_regime,
        f0_stance=f0_stance,
        holdings=holdings,
        provisional=provisional,
        analog=analog_map,
        mandate=rules,
        as_of=as_of,
        overlays_raw=overlays,
        previous=previous,
        horizons=horizons,
    )


def clip_holdings(holdings: list[HoldingRow] | None, decision: Decision) -> list[HoldingRow]:
    """Executable holdings for tables: Decision weights, original tape kept."""
    return apply_decision_to_holdings(holdings, decision)


def decision_after_forecast(
    *,
    market_regime: str | None,
    f0_stance: str,
    holdings: list[HoldingRow] | None,
    provisional: bool,
    as_of: str | None,
    forecast_payload: Mapping[str, Any],
    previous: Decision | None,
    overlays_reason: str | None = None,
    mandate: ProcessMandate | None = None,
    horizons: Mapping[str, Any] | None = None,
) -> Decision:
    """Second compose: analog may only tighten the daily Decision."""
    calls = analog_calls_from_forecast(forecast_payload)
    trend = forecast_payload.get("trend") if isinstance(forecast_payload, Mapping) else None
    if horizons is None and isinstance(trend, Mapping):
        blob = trend.get("horizons")
        if isinstance(blob, Mapping):
            horizons = blob
    return compose(
        market_regime=market_regime,
        f0_stance=f0_stance,
        holdings=holdings,
        provisional=provisional,
        analog=calls,
        mandate=mandate or DEFAULT_MANDATE,
        as_of=as_of,
        overlays_raw={"reason": overlays_reason or "", "f0_stance": f0_stance},
        previous=previous,
        horizons=horizons,
    )
