"""Extract analog overlay calls from a monitor forecast payload."""

from __future__ import annotations

from typing import Any, Mapping

from src.process.models import AnalogCall


def analog_calls_from_forecast(payload: Mapping[str, Any] | None) -> dict[str, AnalogCall]:
    """Read per-symbol directional calls from ``run_monitor_forecast`` output.

    Missing / uncalled items are omitted so compose treats them as 'no analog
    opinion' rather than a buy signal.
    """
    if not isinstance(payload, Mapping):
        return {}
    out: dict[str, AnalogCall] = {}
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        assessment = item.get("assessment") if isinstance(item.get("assessment"), Mapping) else {}
        called = bool(assessment.get("called"))
        bias = str(assessment.get("bias") or "neutral").strip().lower() or "neutral"
        veto = assessment.get("market_veto")
        veto_text = str(veto).strip() if veto else None
        confidence = assessment.get("confidence")
        try:
            conf = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            conf = None
        out[symbol] = AnalogCall(
            symbol=symbol,
            bias=bias,
            called=called,
            market_veto=veto_text or None,
            confidence=conf,
        )
    return out
