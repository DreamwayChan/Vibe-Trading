"""Monitor board data shapes (per-strategy boards + daily holdings history)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.process.models import Decision

Action = Literal["buy", "sell", "hold"]
# Implied strategy position from the latest signal (not broker account holdings).
PositionStatus = Literal["long", "flat", "short", "unknown"]
StrategyType = Literal["run_dir", "shadow"]
# Per-name change vs the previous persisted as_of snapshot.
HoldingAction = Literal["enter", "exit", "hold", "add", "trim"]

# Keep the watchlist focused: enough names for personal alerts, not a screener.
# Strategy holdings scan uses the run universe and is not capped by this.
MAX_SYMBOLS = 20

# Weights at or below this count as flat when deriving holdings / ops.
WEIGHT_EPSILON = 1e-9


@dataclass
class StrategyRef:
    """Pointer to the strategy used for scanning.

    Attributes:
        type: ``run_dir`` loads ``agent/runs/<run_id>/code/signal_engine.py``;
            ``shadow`` loads a Shadow Account profile and uses its scanner.
        run_id: Backtest run id when ``type == "run_dir"``.
        shadow_id: Shadow profile id when ``type == "shadow"``.
    """

    type: StrategyType = "run_dir"
    run_id: str | None = None
    shadow_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "run_id": self.run_id,
            "shadow_id": self.shadow_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> StrategyRef:
        if not isinstance(raw, dict):
            return cls()
        kind = str(raw.get("type") or "run_dir").strip().lower()
        if kind not in ("run_dir", "shadow"):
            kind = "run_dir"
        return cls(
            type=kind,  # type: ignore[arg-type]
            run_id=_optional_str(raw.get("run_id")),
            shadow_id=_optional_str(raw.get("shadow_id")),
        )


@dataclass
class MonitorSuggestion:
    """One deterministic action suggestion for a symbol.

    ``as_of`` is the monitoring decision date (scan day). ``data_as_of`` is the
    latest OHLCV/signal bar actually used — may lag on holidays or delayed feeds.
    ``position`` is the strategy-implied stance from the latest signal, not a
    live broker account position.
    """

    symbol: str
    action: Action
    reason: str
    as_of: str
    position: PositionStatus = "unknown"
    data_as_of: str | None = None
    # Latest OHLCV close used for the scan — sanity-check that data is fresh.
    price: float | None = None
    # True when today's bar was synthesized from a live quote (not official EOD).
    provisional: bool = False
    confidence: float | None = None
    signal: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Always keep provisional so the UI can show/hide the badge.
        return {k: v for k, v in payload.items() if v is not None or k == "provisional"}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MonitorSuggestion:
        action = str(raw.get("action") or "hold").strip().lower()
        if action not in ("buy", "sell", "hold"):
            action = "hold"
        position = str(raw.get("position") or "unknown").strip().lower()
        if position not in ("long", "flat", "short", "unknown"):
            position = "unknown"
        confidence = raw.get("confidence")
        signal = raw.get("signal")
        price = raw.get("price")
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            action=action,  # type: ignore[arg-type]
            reason=str(raw.get("reason") or "").strip(),
            as_of=str(raw.get("as_of") or "").strip(),
            position=position,  # type: ignore[arg-type]
            data_as_of=_optional_str(raw.get("data_as_of")),
            price=float(price) if price is not None else None,
            provisional=bool(raw.get("provisional", False)),
            confidence=float(confidence) if confidence is not None else None,
            signal=float(signal) if signal is not None else None,
        )


@dataclass
class HoldingRow:
    """One strategy-implied holding (target weight > epsilon)."""

    symbol: str
    weight: float
    action: HoldingAction = "hold"
    price: float | None = None
    data_as_of: str | None = None
    provisional: bool = False
    prev_weight: float | None = None
    # Dual-track plan from trend_regime. Not the same as ``action`` (enter/trim).
    trend: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v is not None or k == "provisional"}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HoldingRow:
        action = str(raw.get("action") or "hold").strip().lower()
        if action not in ("enter", "exit", "hold", "add", "trim"):
            action = "hold"
        weight = raw.get("weight")
        prev_weight = raw.get("prev_weight")
        price = raw.get("price")
        trend_raw = raw.get("trend")
        trend = trend_raw if isinstance(trend_raw, dict) else None
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            weight=float(weight) if weight is not None else 0.0,
            action=action,  # type: ignore[arg-type]
            price=float(price) if price is not None else None,
            data_as_of=_optional_str(raw.get("data_as_of")),
            provisional=bool(raw.get("provisional", False)),
            prev_weight=float(prev_weight) if prev_weight is not None else None,
            trend=trend,
        )


@dataclass
class DayOps:
    """Portfolio churn between the previous as_of snapshot and today."""

    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entered": list(self.entered),
            "exited": list(self.exited),
            "held": list(self.held),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DayOps:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            entered=_symbol_list(raw.get("entered")),
            exited=_symbol_list(raw.get("exited")),
            held=_symbol_list(raw.get("held")),
        )


@dataclass
class MonitorDaySnapshot:
    """Persistable daily portfolio snapshot for one strategy run."""

    as_of: str
    scanned_at: str
    run_id: str
    provisional: bool = False
    holdings: list[HoldingRow] = field(default_factory=list)
    ops: DayOps = field(default_factory=DayOps)
    suggestions: list[MonitorSuggestion] = field(default_factory=list)
    universe_size: int = 0
    fetch_error_count: int = 0
    live_quote_ok: int = 0
    live_quote_total: int = 0
    live_quote_coverage: float = 0.0
    decision: Decision | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "as_of": self.as_of,
            "scanned_at": self.scanned_at,
            "run_id": self.run_id,
            "provisional": self.provisional,
            "holdings": [h.to_dict() for h in self.holdings],
            "ops": self.ops.to_dict(),
            "suggestions": [s.to_dict() for s in self.suggestions],
            "universe_size": self.universe_size,
            "fetch_error_count": self.fetch_error_count,
            "live_quote_ok": self.live_quote_ok,
            "live_quote_total": self.live_quote_total,
            "live_quote_coverage": self.live_quote_coverage,
        }
        if self.decision is not None:
            payload["decision"] = self.decision.to_dict()
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> MonitorDaySnapshot | None:
        if not isinstance(raw, dict):
            return None
        as_of = _optional_str(raw.get("as_of"))
        scanned_at = _optional_str(raw.get("scanned_at"))
        run_id = _optional_str(raw.get("run_id"))
        if not as_of or not scanned_at or not run_id:
            return None
        holdings_raw = raw.get("holdings") or []
        holdings: list[HoldingRow] = []
        if isinstance(holdings_raw, list):
            for item in holdings_raw:
                if isinstance(item, dict):
                    holdings.append(HoldingRow.from_dict(item))
        suggestions_raw = raw.get("suggestions") or []
        suggestions: list[MonitorSuggestion] = []
        if isinstance(suggestions_raw, list):
            for item in suggestions_raw:
                if isinstance(item, dict):
                    suggestions.append(MonitorSuggestion.from_dict(item))
        return cls(
            as_of=as_of,
            scanned_at=scanned_at,
            run_id=run_id,
            provisional=bool(raw.get("provisional", False)),
            holdings=holdings,
            ops=DayOps.from_dict(raw.get("ops") if isinstance(raw.get("ops"), dict) else None),
            suggestions=suggestions,
            universe_size=int(raw.get("universe_size") or 0),
            fetch_error_count=int(raw.get("fetch_error_count") or 0),
            live_quote_ok=int(raw.get("live_quote_ok") or 0),
            live_quote_total=int(raw.get("live_quote_total") or 0),
            live_quote_coverage=float(raw.get("live_quote_coverage") or 0.0),
            decision=Decision.from_dict(raw.get("decision") if isinstance(raw.get("decision"), dict) else None),
        )


@dataclass
class MonitorConfig:
    """One strategy-bound monitor board (watchlist + latest scan cache).

    Persistence is one file per strategy source (see ``store.board_id_for_strategy``).
    Switching strategies loads a different board; it must not reuse another
    source's symbols or suggestions.
    """

    symbols: list[str] = field(default_factory=list)
    strategy: StrategyRef = field(default_factory=StrategyRef)
    last_scan_at: str | None = None
    suggestions: list[MonitorSuggestion] = field(default_factory=list)
    # Latest strategy holdings + day ops (mirrors the newest history snapshot).
    holdings: list[HoldingRow] = field(default_factory=list)
    ops: DayOps = field(default_factory=DayOps)
    # Optional cron/interval string mirrored into scheduled-runs when enabled.
    schedule: str | None = None
    # Last composed instruction (engine holdings stay unclipped for tomorrow's baseline).
    decision: Decision | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "symbols": list(self.symbols),
            "strategy": self.strategy.to_dict(),
            "last_scan_at": self.last_scan_at,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "holdings": [h.to_dict() for h in self.holdings],
            "ops": self.ops.to_dict(),
            "schedule": self.schedule,
        }
        if self.decision is not None:
            payload["decision"] = self.decision.to_dict()
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> MonitorConfig:
        if not isinstance(raw, dict):
            return cls()
        symbols = _normalize_symbols(raw.get("symbols") or [])
        suggestions_raw = raw.get("suggestions") or []
        suggestions: list[MonitorSuggestion] = []
        if isinstance(suggestions_raw, list):
            for item in suggestions_raw:
                if isinstance(item, dict):
                    suggestions.append(MonitorSuggestion.from_dict(item))
        holdings_raw = raw.get("holdings") or []
        holdings: list[HoldingRow] = []
        if isinstance(holdings_raw, list):
            for item in holdings_raw:
                if isinstance(item, dict):
                    holdings.append(HoldingRow.from_dict(item))
        return cls(
            symbols=symbols,
            strategy=StrategyRef.from_dict(
                raw.get("strategy") if isinstance(raw.get("strategy"), dict) else None
            ),
            last_scan_at=_optional_str(raw.get("last_scan_at")),
            suggestions=suggestions,
            holdings=holdings,
            ops=DayOps.from_dict(raw.get("ops") if isinstance(raw.get("ops"), dict) else None),
            schedule=_optional_str(raw.get("schedule")),
            decision=Decision.from_dict(raw.get("decision") if isinstance(raw.get("decision"), dict) else None),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _symbol_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _normalize_symbols(raw: Any) -> list[str]:
    """Deduplicate and cap watchlist symbols; preserve first-seen order."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= MAX_SYMBOLS:
            break
    return out
