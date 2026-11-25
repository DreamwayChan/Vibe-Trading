"""Lightweight personal brokerage portfolio store (我的真实持仓) for /monitor.

Maintains the user's real capital, focus slot limit (default 3 names), habitual
max stop-loss (-10%), per-trade capital risk budget (1.5%), and current held
lots so Monitor can translate abstract model weights into concrete share deltas.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.RLock()
_BARE_CODE_RE = re.compile(r"^\d{6}$")


def normalize_ashare_symbol(raw: Any) -> str:
    """Normalize ``600519`` or ``600519.sh`` into ``600519.SH``."""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if _BARE_CODE_RE.match(text):
        if text.startswith(("6", "5", "900")):
            return f"{text}.SH"
        if text.startswith(("8", "4", "92")):
            return f"{text}.BJ"
        return f"{text}.SZ"
    return text


@dataclass
class RealPosition:
    """One position currently held in the user's real brokerage account."""

    symbol: str
    shares: int
    cost_price: float
    name: str = ""
    buy_date: str = ""
    stop_price: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> RealPosition | None:
        if not isinstance(raw, Mapping):
            return None
        symbol = normalize_ashare_symbol(raw.get("symbol"))
        if not symbol:
            return None
        try:
            shares = int(float(raw.get("shares") or 0))
            cost_price = float(raw.get("cost_price") or 0.0)
        except (TypeError, ValueError):
            return None
        if shares <= 0 or cost_price <= 0 or cost_price != cost_price:
            return None
        stop_raw = raw.get("stop_price")
        stop_val: float | None = None
        if stop_raw not in (None, ""):
            try:
                parsed = float(stop_raw)
                if parsed > 0 and parsed == parsed:
                    stop_val = round(parsed, 3)
            except (TypeError, ValueError):
                stop_val = None
        return cls(
            symbol=symbol,
            shares=shares,
            cost_price=round(cost_price, 4),
            name=str(raw.get("name") or "").strip(),
            buy_date=str(raw.get("buy_date") or "").strip()[:10],
            stop_price=stop_val,
            note=str(raw.get("note") or "").strip(),
        )


@dataclass
class MyPortfolio:
    """User's personal account settings and live holdings maintained on /monitor."""

    total_capital: float = 200_000.0
    available_cash: float | None = None
    max_positions: int = 3
    max_stop_loss_pct: float = 0.10
    risk_per_trade_pct: float = 0.015
    min_trade_amount: float = 10_000.0
    min_rebalance_delta_pct: float = 0.03
    positions: list[RealPosition] = field(default_factory=list)
    updated_at: str = ""

    def effective_cash(self, mark_prices: Mapping[str, float] | None = None) -> float:
        """Return explicit ``available_cash`` if set, else ``total_capital - cost_basis``."""
        if self.available_cash is not None and self.available_cash >= 0:
            return float(self.available_cash)
        prices = mark_prices or {}
        invested = 0.0
        for pos in self.positions:
            px = prices.get(pos.symbol)
            ref = px if (px is not None and px > 0) else pos.cost_price
            invested += pos.shares * ref
        return max(0.0, float(self.total_capital) - invested)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_capital": round(float(self.total_capital), 2),
            "available_cash": (
                round(float(self.available_cash), 2)
                if self.available_cash is not None
                else None
            ),
            "effective_cash": round(self.effective_cash(), 2),
            "max_positions": int(self.max_positions),
            "max_stop_loss_pct": round(float(self.max_stop_loss_pct), 4),
            "risk_per_trade_pct": round(float(self.risk_per_trade_pct), 4),
            "min_trade_amount": round(float(self.min_trade_amount), 2),
            "min_rebalance_delta_pct": round(float(self.min_rebalance_delta_pct), 4),
            "positions": [p.to_dict() for p in self.positions],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> MyPortfolio:
        if not isinstance(raw, Mapping):
            return cls()
        total_capital = _clamp_float(raw.get("total_capital"), default=200_000.0, low=1_000.0, high=1e9)
        avail_raw = raw.get("available_cash")
        available_cash: float | None = None
        if avail_raw not in (None, ""):
            try:
                val = float(avail_raw)
                if val >= 0 and val == val:
                    available_cash = val
            except (TypeError, ValueError):
                available_cash = None
        max_positions = int(_clamp_float(raw.get("max_positions"), default=3.0, low=1.0, high=10.0))
        max_stop = _clamp_float(raw.get("max_stop_loss_pct"), default=0.10, low=0.02, high=0.30)
        risk_pct = _clamp_float(raw.get("risk_per_trade_pct"), default=0.015, low=0.002, high=0.10)
        min_amt = _clamp_float(raw.get("min_trade_amount"), default=10_000.0, low=0.0, high=1e7)
        min_delta = _clamp_float(raw.get("min_rebalance_delta_pct"), default=0.03, low=0.0, high=0.50)

        deduped: dict[str, RealPosition] = {}
        for item in raw.get("positions") or []:
            pos = RealPosition.from_dict(item if isinstance(item, Mapping) else None)
            if pos is not None:
                deduped[pos.symbol] = pos

        return cls(
            total_capital=total_capital,
            available_cash=available_cash,
            max_positions=max_positions,
            max_stop_loss_pct=max_stop,
            risk_per_trade_pct=risk_pct,
            min_trade_amount=min_amt,
            min_rebalance_delta_pct=min_delta,
            positions=list(deduped.values()),
            updated_at=str(raw.get("updated_at") or ""),
        )


def _clamp_float(value: Any, *, default: float, low: float, high: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return max(low, min(high, num))


def portfolio_path() -> Path:
    return Path.home() / ".vibe-trading" / "monitor" / "my_portfolio.json"


def load_my_portfolio() -> MyPortfolio:
    with _LOCK:
        path = portfolio_path()
        if not path.is_file():
            return MyPortfolio()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return MyPortfolio()
        return MyPortfolio.from_dict(raw if isinstance(raw, Mapping) else None)


def save_my_portfolio(portfolio: MyPortfolio) -> MyPortfolio:
    with _LOCK:
        path = portfolio_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        portfolio.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(portfolio.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".my_portfolio_", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return portfolio
