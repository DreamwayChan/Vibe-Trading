"""Persisted paper-book state and the daily report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PAPER_BOOK_DEVIATION = "paper_book_deviation"


@dataclass
class Lot:
    """Shares bought on one session. T+1 locks ``buy_date``."""

    shares: int
    buy_date: str
    price: float

    def to_dict(self) -> dict[str, Any]:
        return {"shares": int(self.shares), "buy_date": self.buy_date, "price": self.price}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Lot:
        return cls(
            shares=int(raw.get("shares") or 0),
            buy_date=str(raw.get("buy_date") or ""),
            price=float(raw.get("price") or 0.0),
        )


@dataclass
class PositionState:
    """Open lots for one symbol, marked at the last known close."""

    symbol: str
    lots: list[Lot] = field(default_factory=list)
    last_price: float = 0.0

    @property
    def shares(self) -> int:
        return sum(lot.shares for lot in self.lots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "lots": [lot.to_dict() for lot in self.lots],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PositionState:
        lots_raw = raw.get("lots") or []
        lots = [Lot.from_dict(item) for item in lots_raw if isinstance(item, dict)]
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            lots=lots,
            last_price=float(raw.get("last_price") or 0.0),
        )


@dataclass
class BookState:
    """Cash, lots, and the checkpoint used to restep the latest session."""

    book_id: str
    run_id: str = ""
    initial_cash: float = 1_000_000.0
    cash: float = 1_000_000.0
    positions: dict[str, PositionState] = field(default_factory=dict)
    settled_through: str | None = None
    opening: dict[str, Any] | None = None
    inception_date: str | None = None
    anchor_date: str | None = None
    anchor_equity: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "book_id": self.book_id,
            "run_id": self.run_id,
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "positions": {symbol: pos.to_dict() for symbol, pos in self.positions.items()},
            "settled_through": self.settled_through,
            "opening": self.opening,
            "inception_date": self.inception_date,
            "anchor_date": self.anchor_date,
            "anchor_equity": self.anchor_equity,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BookState:
        if not isinstance(raw, dict):
            return cls(book_id="")
        positions: dict[str, PositionState] = {}
        raw_positions = raw.get("positions") or {}
        if isinstance(raw_positions, dict):
            for key, value in raw_positions.items():
                if isinstance(value, dict):
                    pos = PositionState.from_dict(value)
                    symbol = pos.symbol or str(key).strip().upper()
                    pos.symbol = symbol
                    positions[symbol] = pos
        opening = raw.get("opening") if isinstance(raw.get("opening"), dict) else None
        anchor = raw.get("anchor_equity")
        return cls(
            book_id=str(raw.get("book_id") or ""),
            run_id=str(raw.get("run_id") or ""),
            initial_cash=float(raw.get("initial_cash") or 0.0),
            cash=float(raw.get("cash") or 0.0),
            positions=positions,
            settled_through=_optional_str(raw.get("settled_through")),
            opening=opening,
            inception_date=_optional_str(raw.get("inception_date")),
            anchor_date=_optional_str(raw.get("anchor_date")),
            anchor_equity=float(anchor) if anchor is not None else None,
        )


@dataclass(frozen=True)
class Intent:
    """One target for the session, already restricted to tradable signals."""

    symbol: str
    action: str
    target_weight: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "target_weight": self.target_weight,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Intent:
        action = str(raw.get("action") or "").strip().lower()
        if action not in ("buy", "sell"):
            action = "buy"
        return cls(
            symbol=str(raw.get("symbol") or "").strip().upper(),
            action=action,
            target_weight=float(raw.get("target_weight") or 0.0),
        )


@dataclass
class Fill:
    symbol: str
    side: str
    shares: int
    close: float
    ref_price: float
    fill_price: float
    commission: float
    notional: float
    slippage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "shares": self.shares,
            "close": self.close,
            "ref_price": self.ref_price,
            "fill_price": self.fill_price,
            "commission": self.commission,
            "notional": self.notional,
            "slippage": self.slippage,
        }


@dataclass
class Unfilled:
    symbol: str
    side: str
    shares: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "shares": self.shares,
            "reason": self.reason,
        }


@dataclass
class DailyReport:
    """One settled session: NAV, fills, and the gap versus the bound backtest."""

    book_id: str
    run_id: str
    as_of: str
    nav: float
    cash: float
    market_value: float
    initial_cash: float
    prev_nav: float
    daily_return: float
    cum_return: float
    turnover: float
    fills: list[Fill] = field(default_factory=list)
    unfilled: list[Unfilled] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    intents: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    expectation: dict[str, Any] = field(default_factory=dict)
    downgraded: bool = False
    downgrade_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "run_id": self.run_id,
            "as_of": self.as_of,
            "nav": self.nav,
            "cash": self.cash,
            "market_value": self.market_value,
            "initial_cash": self.initial_cash,
            "prev_nav": self.prev_nav,
            "daily_return": self.daily_return,
            "cum_return": self.cum_return,
            "turnover": self.turnover,
            "fills": [fill.to_dict() for fill in self.fills],
            "unfilled": [row.to_dict() for row in self.unfilled],
            "positions": list(self.positions),
            "intents": list(self.intents),
            "skipped": list(self.skipped),
            "expectation": dict(self.expectation),
            "downgraded": self.downgraded,
            "downgrade_reason": self.downgrade_reason,
        }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
