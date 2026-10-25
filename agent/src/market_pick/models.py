"""Data shapes for the market-pick board screen."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Stance = Literal["full", "half_watch", "none"]
BoardType = Literal["industry", "concept"]


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
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


@dataclass
class BoardScore:
    """One Eastmoney board after dual-track aggregation."""

    bk: str
    name: str
    board_type: BoardType
    classified: int = 0
    universe_size: int = 0
    rs_n: int = 0
    dip_n: int = 0
    knife_n: int = 0
    wait_n: int = 0
    sell_n: int = 0
    rs_share: float | None = None
    knife_share: float | None = None
    live_score: float | None = None
    rank: int | None = None
    ret_20d: float | None = None
    timing_mult: float | None = None
    ew_ma20_ratio: float | None = None
    skip_reason: str | None = None
    catalog_key: str | None = None
    catalog_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v is not None or k in ("bk", "name")}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BoardScore | None:
        if not isinstance(raw, dict):
            return None
        bk = _optional_str(raw.get("bk"))
        name = _optional_str(raw.get("name"))
        if not bk or not name:
            return None
        board_type = str(raw.get("board_type") or "industry").strip().lower()
        if board_type not in ("industry", "concept"):
            board_type = "industry"
        return cls(
            bk=bk.upper(),
            name=name,
            board_type=board_type,  # type: ignore[arg-type]
            classified=int(raw.get("classified") or 0),
            universe_size=int(raw.get("universe_size") or 0),
            rs_n=int(raw.get("rs_n") or 0),
            dip_n=int(raw.get("dip_n") or 0),
            knife_n=int(raw.get("knife_n") or 0),
            wait_n=int(raw.get("wait_n") or 0),
            sell_n=int(raw.get("sell_n") or 0),
            rs_share=_optional_float(raw.get("rs_share")),
            knife_share=_optional_float(raw.get("knife_share")),
            live_score=_optional_float(raw.get("live_score")),
            rank=_optional_int(raw.get("rank")),
            ret_20d=_optional_float(raw.get("ret_20d")),
            timing_mult=_optional_float(raw.get("timing_mult")),
            ew_ma20_ratio=_optional_float(raw.get("ew_ma20_ratio")),
            skip_reason=_optional_str(raw.get("skip_reason")),
            catalog_key=_optional_str(raw.get("catalog_key")),
            catalog_name=_optional_str(raw.get("catalog_name")),
        )


@dataclass
class MarketPickResult:
    """One calendar-day screen: CSI 300 overlay plus ranked boards."""

    as_of: str
    scanned_at: str
    market_regime: str | None = None
    mom120: float | None = None
    stance: Stance = "none"
    reason: str = ""
    recommended_bk: str | None = None
    map_bk: str | None = None
    unique_symbols: int = 0
    ohlcv_ok: int = 0
    fetch_error_count: int = 0
    boards: list[BoardScore] = field(default_factory=list)
    # Short / medium / long CSI 300 split. Medium == market_regime.
    horizons: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "as_of": self.as_of,
            "scanned_at": self.scanned_at,
            "market_regime": self.market_regime,
            "mom120": self.mom120,
            "stance": self.stance,
            "reason": self.reason,
            "recommended_bk": self.recommended_bk,
            "map_bk": self.map_bk,
            "unique_symbols": self.unique_symbols,
            "ohlcv_ok": self.ohlcv_ok,
            "fetch_error_count": self.fetch_error_count,
            "boards": [row.to_dict() for row in self.boards],
        }
        if self.horizons:
            payload["horizons"] = dict(self.horizons)
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> MarketPickResult | None:
        if not isinstance(raw, dict):
            return None
        as_of = _optional_str(raw.get("as_of"))
        scanned_at = _optional_str(raw.get("scanned_at"))
        if not as_of or not scanned_at:
            return None
        stance = str(raw.get("stance") or "none").strip().lower()
        if stance not in ("full", "half_watch", "none"):
            stance = "none"
        boards: list[BoardScore] = []
        for item in raw.get("boards") or []:
            if isinstance(item, dict):
                row = BoardScore.from_dict(item)
                if row is not None:
                    boards.append(row)
        return cls(
            as_of=as_of,
            scanned_at=scanned_at,
            market_regime=_optional_str(raw.get("market_regime")),
            mom120=_optional_float(raw.get("mom120")),
            stance=stance,  # type: ignore[arg-type]
            reason=str(raw.get("reason") or ""),
            recommended_bk=_optional_str(raw.get("recommended_bk")),
            map_bk=_optional_str(raw.get("map_bk")),
            unique_symbols=int(raw.get("unique_symbols") or 0),
            ohlcv_ok=int(raw.get("ohlcv_ok") or 0),
            fetch_error_count=int(raw.get("fetch_error_count") or 0),
            boards=boards,
            horizons=raw.get("horizons") if isinstance(raw.get("horizons"), dict) else None,
        )
