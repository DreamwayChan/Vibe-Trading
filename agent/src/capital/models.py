"""Data shapes for the A-share capital-flow monitor.

All monetary values are in CNY (yuan) unless a field name says otherwise —
the Web UI formats them into 亿元. Percentages are percentages (``6.35`` means
6.35%), basis points are basis points (``12.4`` means 12.4bp).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Exchange = Literal["SH", "SZ"]
McapMode = Literal["circ_mv", "weight_implied"]
# Deterministic state label for the national-team proxy.
FlowRegime = Literal["surge", "inflow", "neutral", "outflow", "drain"]


def _f(value: Any) -> float | None:
    """Coerce to float, treating NaN/inf/None as missing."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class EtfFundDef:
    """One CSI 300 index fund tracked by the monitor.

    ``code`` is the 6-digit exchange code. ``exchange`` decides which bourse
    publishes the daily shares (``SH`` → sse.com.cn, ``SZ`` → szse.cn).
    """

    code: str
    name: str
    exchange: Exchange = "SH"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.name, "exchange": self.exchange}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EtfFundDef:
        exchange = str(raw.get("exchange") or "SH").upper()
        if exchange not in ("SH", "SZ"):
            exchange = "SH"
        return cls(
            code=str(raw.get("code") or "").strip(),
            name=str(raw.get("name") or "").strip(),
            exchange=exchange,  # type: ignore[arg-type]
        )


@dataclass
class FundDailyRow:
    """Per-fund state on one trading day."""

    code: str
    name: str
    exchange: Exchange = "SH"
    # Fund shares outstanding (份).
    shares: float | None = None
    shares_prev: float | None = None
    # Unit NAV (单位净值, 元/份).
    nav: float | None = None
    nav_prev: float | None = None
    # shares × nav.
    aum: float | None = None
    aum_prev: float | None = None
    # (shares - shares_prev) × nav_prev — the money that actually entered.
    net_flow: float | None = None
    # shares_prev × (nav - nav_prev) — mark-to-market move, not a flow.
    nav_pnl: float | None = None
    # Share of the tracked group's AUM (0-1).
    group_weight: float | None = None
    # True when shares were carried forward because the bourse had not
    # published this fund for the day yet.
    stale: bool = False

    @property
    def shares_change(self) -> float | None:
        if self.shares is None or self.shares_prev is None:
            return None
        return self.shares - self.shares_prev

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "name": self.name,
            "exchange": self.exchange,
            "shares": self.shares,
            "shares_prev": self.shares_prev,
            "shares_change": self.shares_change,
            "nav": self.nav,
            "nav_prev": self.nav_prev,
            "aum": self.aum,
            "aum_prev": self.aum_prev,
            "net_flow": self.net_flow,
            "nav_pnl": self.nav_pnl,
            "group_weight": self.group_weight,
            "stale": self.stale,
        }
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FundDailyRow:
        exchange = str(raw.get("exchange") or "SH").upper()
        if exchange not in ("SH", "SZ"):
            exchange = "SH"
        return cls(
            code=str(raw.get("code") or "").strip(),
            name=str(raw.get("name") or "").strip(),
            exchange=exchange,  # type: ignore[arg-type]
            shares=_f(raw.get("shares")),
            shares_prev=_f(raw.get("shares_prev")),
            nav=_f(raw.get("nav")),
            nav_prev=_f(raw.get("nav_prev")),
            aum=_f(raw.get("aum")),
            aum_prev=_f(raw.get("aum_prev")),
            net_flow=_f(raw.get("net_flow")),
            nav_pnl=_f(raw.get("nav_pnl")),
            group_weight=_f(raw.get("group_weight")),
            stale=bool(raw.get("stale", False)),
        )


@dataclass
class CapitalPoint:
    """One day of the ETF-OI series."""

    date: str
    # Aggregate AUM of the tracked CSI 300 funds.
    aum: float | None = None
    shares_total: float | None = None
    index_close: float | None = None
    # CSI 300 free-float market cap implied for this day.
    index_mcap: float | None = None
    # aum / index_mcap × 100.
    oi_pct: float | None = None
    # Daily net subscription in CNY. ``None`` when the day is incomplete.
    net_flow: float | None = None
    nav_pnl: float | None = None
    # net_flow / index_mcap in basis points.
    flow_bp: float | None = None
    fund_count: int = 0
    missing_count: int = 0
    # Shown but not settled yet (bourse shares not fully published).
    provisional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "aum": self.aum,
            "shares_total": self.shares_total,
            "index_close": self.index_close,
            "index_mcap": self.index_mcap,
            "oi_pct": self.oi_pct,
            "net_flow": self.net_flow,
            "nav_pnl": self.nav_pnl,
            "flow_bp": self.flow_bp,
            "fund_count": self.fund_count,
            "missing_count": self.missing_count,
            "provisional": self.provisional,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CapitalPoint:
        return cls(
            date=str(raw.get("date") or "").strip(),
            aum=_f(raw.get("aum")),
            shares_total=_f(raw.get("shares_total")),
            index_close=_f(raw.get("index_close")),
            index_mcap=_f(raw.get("index_mcap")),
            oi_pct=_f(raw.get("oi_pct")),
            net_flow=_f(raw.get("net_flow")),
            nav_pnl=_f(raw.get("nav_pnl")),
            flow_bp=_f(raw.get("flow_bp")),
            fund_count=int(raw.get("fund_count") or 0),
            missing_count=int(raw.get("missing_count") or 0),
            provisional=bool(raw.get("provisional", False)),
        )


@dataclass
class McapReference:
    """How the CSI 300 free-float market cap denominator was calibrated.

    The index is a free-float weighted Paasche index, so its total adjusted
    market cap is proportional to the index level between rebalances:
    ``mcap_t ≈ per_point × index_close_t``. ``per_point`` is pinned once per
    day from the constituent snapshot and then carried across history.
    """

    date: str = ""
    weights_date: str = ""
    index_close: float | None = None
    per_point: float | None = None
    total: float | None = None
    mode: McapMode = "circ_mv"
    # Same snapshot measured the other way — kept for the methodology panel.
    total_circ_mv: float | None = None
    total_weight_implied: float | None = None
    constituents: int = 0
    covered: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weights_date": self.weights_date,
            "index_close": self.index_close,
            "per_point": self.per_point,
            "total": self.total,
            "mode": self.mode,
            "total_circ_mv": self.total_circ_mv,
            "total_weight_implied": self.total_weight_implied,
            "constituents": self.constituents,
            "covered": self.covered,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> McapReference:
        mode = str(raw.get("mode") or "circ_mv")
        if mode not in ("circ_mv", "weight_implied"):
            mode = "circ_mv"
        return cls(
            date=str(raw.get("date") or ""),
            weights_date=str(raw.get("weights_date") or ""),
            index_close=_f(raw.get("index_close")),
            per_point=_f(raw.get("per_point")),
            total=_f(raw.get("total")),
            mode=mode,  # type: ignore[arg-type]
            total_circ_mv=_f(raw.get("total_circ_mv")),
            total_weight_implied=_f(raw.get("total_weight_implied")),
            constituents=int(raw.get("constituents") or 0),
            covered=int(raw.get("covered") or 0),
        )


@dataclass
class FlowStats:
    """Aggregates shown on the KPI strip and the state card."""

    oi_pct: float | None = None
    # Day-over-day change of the ratio, in bp.
    oi_change_bp: float | None = None
    aum: float | None = None
    aum_change: float | None = None
    net_flow: float | None = None
    flow_5d: float | None = None
    flow_20d: float | None = None
    # Cumulative net subscription over the whole loaded window.
    flow_window: float | None = None
    # Net subscription normalised by the index market cap, in bp.
    flow_5d_bp: float | None = None
    flow_20d_bp: float | None = None
    # z-score of the daily flow_bp against the trailing window.
    flow_z: float | None = None
    # Positive → consecutive subscription days; negative → redemption days.
    inflow_streak: int = 0
    # Percentile (0-100) of the latest oi_pct inside the window.
    oi_percentile: float | None = None
    regime: FlowRegime = "neutral"
    # 0-100 composite "capital temperature" reading.
    score: float | None = None
    observed_days: int = 0
    flow_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "oi_pct": self.oi_pct,
            "oi_change_bp": self.oi_change_bp,
            "aum": self.aum,
            "aum_change": self.aum_change,
            "net_flow": self.net_flow,
            "flow_5d": self.flow_5d,
            "flow_20d": self.flow_20d,
            "flow_window": self.flow_window,
            "flow_5d_bp": self.flow_5d_bp,
            "flow_20d_bp": self.flow_20d_bp,
            "flow_z": self.flow_z,
            "inflow_streak": self.inflow_streak,
            "oi_percentile": self.oi_percentile,
            "regime": self.regime,
            "score": self.score,
            "observed_days": self.observed_days,
            "flow_days": self.flow_days,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FlowStats:
        regime = str(raw.get("regime") or "neutral")
        if regime not in ("surge", "inflow", "neutral", "outflow", "drain"):
            regime = "neutral"
        return cls(
            oi_pct=_f(raw.get("oi_pct")),
            oi_change_bp=_f(raw.get("oi_change_bp")),
            aum=_f(raw.get("aum")),
            aum_change=_f(raw.get("aum_change")),
            net_flow=_f(raw.get("net_flow")),
            flow_5d=_f(raw.get("flow_5d")),
            flow_20d=_f(raw.get("flow_20d")),
            flow_window=_f(raw.get("flow_window")),
            flow_5d_bp=_f(raw.get("flow_5d_bp")),
            flow_20d_bp=_f(raw.get("flow_20d_bp")),
            flow_z=_f(raw.get("flow_z")),
            inflow_streak=int(raw.get("inflow_streak") or 0),
            oi_percentile=_f(raw.get("oi_percentile")),
            regime=regime,  # type: ignore[arg-type]
            score=_f(raw.get("score")),
            observed_days=int(raw.get("observed_days") or 0),
            flow_days=int(raw.get("flow_days") or 0),
        )


@dataclass
class EtfOiReport:
    """Full payload for the capital-flow page."""

    as_of: str = ""
    built_at: str = ""
    window: int = 0
    points: list[CapitalPoint] = field(default_factory=list)
    funds: list[FundDailyRow] = field(default_factory=list)
    stats: FlowStats = field(default_factory=FlowStats)
    mcap: McapReference = field(default_factory=McapReference)
    # Data-source + methodology notes rendered in the UI's methodology panel.
    methodology: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provisional: bool = False
    universe_size: int = 0
    # True when served from the on-disk series without a network rebuild.
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "built_at": self.built_at,
            "window": self.window,
            "points": [p.to_dict() for p in self.points],
            "funds": [f.to_dict() for f in self.funds],
            "stats": self.stats.to_dict(),
            "mcap": self.mcap.to_dict(),
            "methodology": dict(self.methodology),
            "warnings": list(self.warnings),
            "provisional": self.provisional,
            "universe_size": self.universe_size,
            "cached": self.cached,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EtfOiReport:
        points = [
            CapitalPoint.from_dict(item)
            for item in (raw.get("points") or [])
            if isinstance(item, dict)
        ]
        funds = [
            FundDailyRow.from_dict(item)
            for item in (raw.get("funds") or [])
            if isinstance(item, dict)
        ]
        methodology = raw.get("methodology")
        return cls(
            as_of=str(raw.get("as_of") or ""),
            built_at=str(raw.get("built_at") or ""),
            window=int(raw.get("window") or 0),
            points=points,
            funds=funds,
            stats=FlowStats.from_dict(raw.get("stats") or {}),
            mcap=McapReference.from_dict(raw.get("mcap") or {}),
            methodology=methodology if isinstance(methodology, dict) else {},
            warnings=[str(w) for w in (raw.get("warnings") or [])],
            provisional=bool(raw.get("provisional", False)),
            universe_size=int(raw.get("universe_size") or 0),
            cached=bool(raw.get("cached", False)),
        )
