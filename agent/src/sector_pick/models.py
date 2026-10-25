"""Data shapes for the sector-pick board (catalog rows + live scan result)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.monitor.models import DayOps, HoldingRow
from src.process.models import Decision

Stance = Literal["full", "half_watch", "none"]


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


@dataclass
class SectorScore:
    """Live regime + historical quality for one catalog sector."""

    key: str
    name: str
    panel: str
    run_id: str
    variant: str
    enabled: bool = True
    timing_mult: float | None = None
    ew_ma20_ratio: float | None = None
    ret_20d: float | None = None
    confirm_pass_rate: float | None = None
    live_score: float | None = None
    rank: int | None = None
    universe_size: int = 0
    fetch_error_count: int = 0
    excess_return: float | None = None
    sharpe: float | None = None
    error: str | None = None
    # Filled only after a SignalEngine probe. None = this row was not probed.
    holding_count: int | None = None
    max_weight: float | None = None
    book_healthy: bool | None = None
    skip_reason: str | None = None
    # Dual-track overlay (CSI 300 × names). None shares = panel too short / no index.
    rs_n: int = 0
    dip_n: int = 0
    knife_n: int = 0
    wait_n: int = 0
    sell_n: int = 0
    rs_share: float | None = None
    knife_share: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v is not None or k in ("enabled", "error")}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SectorScore | None:
        if not isinstance(raw, dict):
            return None
        key = _optional_str(raw.get("key"))
        name = _optional_str(raw.get("name"))
        run_id = _optional_str(raw.get("run_id"))
        if not key or not name or not run_id:
            return None
        return cls(
            key=key,
            name=name,
            panel=str(raw.get("panel") or ""),
            run_id=run_id,
            variant=str(raw.get("variant") or "f0_prime"),
            enabled=bool(raw.get("enabled", True)),
            timing_mult=_optional_float(raw.get("timing_mult")),
            ew_ma20_ratio=_optional_float(raw.get("ew_ma20_ratio")),
            ret_20d=_optional_float(raw.get("ret_20d")),
            confirm_pass_rate=_optional_float(raw.get("confirm_pass_rate")),
            live_score=_optional_float(raw.get("live_score")),
            rank=int(raw["rank"]) if raw.get("rank") is not None else None,
            universe_size=int(raw.get("universe_size") or 0),
            fetch_error_count=int(raw.get("fetch_error_count") or 0),
            excess_return=_optional_float(raw.get("excess_return")),
            sharpe=_optional_float(raw.get("sharpe")),
            error=_optional_str(raw.get("error")),
            holding_count=_optional_int(raw.get("holding_count")),
            max_weight=_optional_float(raw.get("max_weight")),
            book_healthy=_optional_bool(raw.get("book_healthy")),
            skip_reason=_optional_str(raw.get("skip_reason")),
            rs_n=int(raw.get("rs_n") or 0),
            dip_n=int(raw.get("dip_n") or 0),
            knife_n=int(raw.get("knife_n") or 0),
            wait_n=int(raw.get("wait_n") or 0),
            sell_n=int(raw.get("sell_n") or 0),
            rs_share=_optional_float(raw.get("rs_share")),
            knife_share=_optional_float(raw.get("knife_share")),
        )


@dataclass
class SectorPickResult:
    """One scan: ranked sectors plus holdings for the displayed board."""

    as_of: str
    scanned_at: str
    recommended_key: str | None = None
    holdings_key: str | None = None
    stance: Stance = "none"
    reason: str = ""
    sectors: list[SectorScore] = field(default_factory=list)
    holdings: list[HoldingRow] = field(default_factory=list)
    ops: DayOps = field(default_factory=DayOps)
    holdings_run_id: str | None = None
    provisional: bool = False
    market_regime: str | None = None
    mom120: float | None = None
    # Official process instruction. Overlay copy stays in ``reason``.
    decision: Decision | None = None
    # Lock is the order; map_key is rank-1 observation. recommended_key == locked_key.
    locked_key: str | None = None
    map_key: str | None = None
    challenger_key: str | None = None
    challenger_days: int = 0
    challenger_as_of: str | None = None
    lock_event: str | None = None
    lock_reason: str | None = None
    # Short / medium / long CSI 300 split. Medium == market_regime.
    horizons: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "as_of": self.as_of,
            "scanned_at": self.scanned_at,
            "recommended_key": self.recommended_key,
            "holdings_key": self.holdings_key,
            "stance": self.stance,
            "reason": self.reason,
            "sectors": [row.to_dict() for row in self.sectors],
            "holdings": [row.to_dict() for row in self.holdings],
            "ops": self.ops.to_dict(),
            "holdings_run_id": self.holdings_run_id,
            "provisional": self.provisional,
            "market_regime": self.market_regime,
            "mom120": self.mom120,
            "locked_key": self.locked_key,
            "map_key": self.map_key,
            "challenger_key": self.challenger_key,
            "challenger_days": int(self.challenger_days),
            "challenger_as_of": self.challenger_as_of,
            "lock_event": self.lock_event,
            "lock_reason": self.lock_reason,
        }
        if self.horizons:
            payload["horizons"] = dict(self.horizons)
        if self.decision is not None:
            payload["decision"] = self.decision.to_dict()
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SectorPickResult | None:
        if not isinstance(raw, dict):
            return None
        as_of = _optional_str(raw.get("as_of"))
        scanned_at = _optional_str(raw.get("scanned_at"))
        if not as_of or not scanned_at:
            return None
        stance = str(raw.get("stance") or "none").strip().lower()
        if stance not in ("full", "half_watch", "none"):
            stance = "none"
        sectors: list[SectorScore] = []
        for item in raw.get("sectors") or []:
            if isinstance(item, dict):
                row = SectorScore.from_dict(item)
                if row is not None:
                    sectors.append(row)
        holdings: list[HoldingRow] = []
        for item in raw.get("holdings") or []:
            if isinstance(item, dict):
                holdings.append(HoldingRow.from_dict(item))
        return cls(
            as_of=as_of,
            scanned_at=scanned_at,
            recommended_key=_optional_str(raw.get("recommended_key")),
            holdings_key=_optional_str(raw.get("holdings_key")),
            stance=stance,  # type: ignore[arg-type]
            reason=str(raw.get("reason") or ""),
            sectors=sectors,
            holdings=holdings,
            ops=DayOps.from_dict(raw.get("ops") if isinstance(raw.get("ops"), dict) else None),
            holdings_run_id=_optional_str(raw.get("holdings_run_id")),
            provisional=bool(raw.get("provisional", False)),
            market_regime=_optional_str(raw.get("market_regime")),
            mom120=_optional_float(raw.get("mom120")),
            decision=Decision.from_dict(raw.get("decision") if isinstance(raw.get("decision"), dict) else None),
            locked_key=_optional_str(raw.get("locked_key")),
            map_key=_optional_str(raw.get("map_key")),
            challenger_key=_optional_str(raw.get("challenger_key")),
            challenger_days=_optional_int(raw.get("challenger_days")) or 0,
            challenger_as_of=_optional_str(raw.get("challenger_as_of")),
            lock_event=_optional_str(raw.get("lock_event")),
            lock_reason=_optional_str(raw.get("lock_reason")),
            horizons=raw.get("horizons") if isinstance(raw.get("horizons"), dict) else None,
        )


@dataclass
class SectorPickState:
    """Persisted enabled-set plus the last scan (so the page loads instantly)."""

    enabled_keys: list[str] = field(default_factory=list)
    result: SectorPickResult | None = None
    # Survives preview swaps and is the input to the next official scan.
    theme_lock: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled_keys": list(self.enabled_keys),
            "result": self.result.to_dict() if self.result else None,
        }
        if self.theme_lock:
            payload["theme_lock"] = dict(self.theme_lock)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SectorPickState:
        if not isinstance(raw, dict):
            return cls()
        keys_raw = raw.get("enabled_keys")
        keys: list[str] = []
        if isinstance(keys_raw, list):
            seen: set[str] = set()
            for item in keys_raw:
                key = str(item or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                keys.append(key)
        result_raw = raw.get("result")
        lock_raw = raw.get("theme_lock")
        return cls(
            enabled_keys=keys,
            result=SectorPickResult.from_dict(result_raw if isinstance(result_raw, dict) else None),
            theme_lock=dict(lock_raw) if isinstance(lock_raw, dict) else None,
        )
