"""Decision shapes produced by the process compose waterfall."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Portfolio-level verb shown on the hero card.
DecisionAction = Literal["wait", "hold", "enter", "add", "trim", "exit"]
# Which gate closed (or last tightened) the book.
VetoLayer = Literal["mandate", "structure", "f0", "analog", "size"]
GateId = Literal["mandate", "structure", "f0", "analog", "size"]


@dataclass
class AnalogCall:
    """One name's 10-day analog directional call (optional overlay)."""

    symbol: str
    bias: str = "neutral"
    called: bool = False
    market_veto: str | None = None
    confidence: float | None = None

    def blocks_new_risk(self) -> bool:
        """True only when analog is a high-conviction down (or reversed) call.

        An up call never opens a buy — analog has no 'please buy' channel.
        """
        if not self.called:
            return False
        if self.market_veto:
            return True
        return str(self.bias).strip().lower() == "down"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceStep:
    """One waterfall gate, written as thought → conclusion for the UI."""

    gate: GateId
    title: str
    thought: str
    conclusion: str
    passed: bool = True
    skipped: bool = False
    veto: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> TraceStep | None:
        if not isinstance(raw, dict):
            return None
        gate = str(raw.get("gate") or "").strip()
        if gate not in ("mandate", "structure", "f0", "analog", "size"):
            return None
        return cls(
            gate=gate,  # type: ignore[arg-type]
            title=str(raw.get("title") or ""),
            thought=str(raw.get("thought") or ""),
            conclusion=str(raw.get("conclusion") or ""),
            passed=bool(raw.get("passed", True)),
            skipped=bool(raw.get("skipped", False)),
            veto=_optional_str(raw.get("veto")),
        )


@dataclass
class SymbolDecision:
    """Executable target for one name after tightening."""

    symbol: str
    action: DecisionAction
    weight: float
    raw_weight: float
    raw_action: str
    structure_action: str | None = None
    analog_bias: str | None = None
    blocked: bool = False
    block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SymbolDecision | None:
        if not isinstance(raw, dict):
            return None
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        action = str(raw.get("action") or "hold").strip().lower()
        if action not in ("wait", "hold", "enter", "add", "trim", "exit"):
            action = "hold"
        return cls(
            symbol=symbol,
            action=action,  # type: ignore[arg-type]
            weight=_float(raw.get("weight"), 0.0),
            raw_weight=_float(raw.get("raw_weight"), 0.0),
            raw_action=str(raw.get("raw_action") or "hold"),
            structure_action=_optional_str(raw.get("structure_action")),
            analog_bias=_optional_str(raw.get("analog_bias")),
            blocked=bool(raw.get("blocked", False)),
            block_reason=_optional_str(raw.get("block_reason")),
        )


@dataclass
class Decision:
    """Single official instruction for the research loop.

    Overlay originals stay in ``overlays_raw`` for the appendix. The hero
    card may only render ``action``, ``size_nav``, ``reason``, and ``trace``.
    """

    action: DecisionAction
    size_nav: float
    reason: str
    as_of: str | None = None
    mandate_hash: str = ""
    analog_applied: bool = False
    vetoed_by: list[str] = field(default_factory=list)
    per_symbol: list[SymbolDecision] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    overlays_raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "size_nav": self.size_nav,
            "reason": self.reason,
            "as_of": self.as_of,
            "mandate_hash": self.mandate_hash,
            "analog_applied": self.analog_applied,
            "vetoed_by": list(self.vetoed_by),
            "per_symbol": [row.to_dict() for row in self.per_symbol],
            "trace": [step.to_dict() for step in self.trace],
            "overlays_raw": dict(self.overlays_raw),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Decision | None:
        if not isinstance(raw, dict):
            return None
        action = str(raw.get("action") or "wait").strip().lower()
        if action not in ("wait", "hold", "enter", "add", "trim", "exit"):
            action = "wait"
        per_symbol: list[SymbolDecision] = []
        for item in raw.get("per_symbol") or []:
            if isinstance(item, dict):
                row = SymbolDecision.from_dict(item)
                if row is not None:
                    per_symbol.append(row)
        trace: list[TraceStep] = []
        for item in raw.get("trace") or []:
            if isinstance(item, dict):
                step = TraceStep.from_dict(item)
                if step is not None:
                    trace.append(step)
        overlays = raw.get("overlays_raw")
        return cls(
            action=action,  # type: ignore[arg-type]
            size_nav=_float(raw.get("size_nav"), 0.0),
            reason=str(raw.get("reason") or ""),
            as_of=_optional_str(raw.get("as_of")),
            mandate_hash=str(raw.get("mandate_hash") or ""),
            analog_applied=bool(raw.get("analog_applied", False)),
            vetoed_by=[str(v) for v in (raw.get("vetoed_by") or []) if str(v).strip()],
            per_symbol=per_symbol,
            trace=trace,
            overlays_raw=dict(overlays) if isinstance(overlays, dict) else {},
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return number
