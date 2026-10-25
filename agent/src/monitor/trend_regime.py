"""Long-horizon A-share regime for a long-only book.

The holdings walk-forward showed down-continuation near 80% and
up-continuation near 31% *unconditionally*. Split by CSI 300 regime
the picture is more specific:

* Index bull does **not** lift this book (~26% 60-day up) — keep fade on.
* Index chop + a name with its own bull stack *does* continue (~52%).
* Index chop + stock bear is the knife (~17% 60-day up).

Default is therefore an oscillation (buy dips, sell range highs), not a
permanent short. Retail A-share accounts cannot short, so bear never
means "open a short"; it means wait or reduce.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

Regime = Literal["bull", "chop", "bear"]
Swing = Literal["oversold", "mid", "overbought"]
Action = Literal["wait", "buy_dip", "hold", "sell_rally", "reduce"]

MA_FAST = 20
MA_MID = 60
MA_SLOW = 120
MA_SHORT_FAST = 5
MA_SHORT_MID = 10
# ~1-year line. Optional: short tapes still classify long from MA120 + mom120.
MA_YEAR = 250
MOM_BARS = 120
SWING_BARS = 60
# Close in the bottom / top fifth of the recent range.
OVERSOLD_MAX = 0.20
OVERBOUGHT_MIN = 0.80
MIN_BARS = MA_SLOW + 1

# Display / overlay order: relative-strength first, knives last.
ACTION_ORDER: tuple[Action, ...] = ("hold", "buy_dip", "sell_rally", "wait", "reduce")
CSI300 = "000300.SH"


@dataclass(frozen=True)
class StructureSnapshot:
    """Causal MA-stack + 120-day momentum at one bar."""

    regime: Regime
    close: float
    ma20: float
    ma60: float
    ma120: float
    mom120: float


@dataclass(frozen=True)
class HorizonSnapshot:
    """CSI 300 tape split by horizon so 'bear' is not read as a crash call.

    ``medium`` is the existing MA20/60/120 book rule (what sizes the theme).
    ``short`` is 5/10/20 — noise relative to that rule. ``long`` is the 年线
    guard: below the 120-day stack but still above MA250 is not a bear market.
    """

    short: Regime
    medium: Regime
    long: Regime
    close: float
    ma5: float
    ma10: float
    ma20: float
    ma60: float
    ma120: float
    ma250: float | None
    mom120: float
    flip: str


@dataclass(frozen=True)
class TrendPlan:
    """Long-only action once market regime and stock swing are known."""

    market_regime: Regime
    stock_regime: Regime
    swing: Swing
    action: Action
    fade_rallies: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sma(close: np.ndarray, period: int) -> float:
    if len(close) < period:
        return float("nan")
    window = close[-period:]
    if not np.isfinite(window).all() or np.any(window <= 0):
        return float("nan")
    return float(np.mean(window))


def classify_structure(close: pd.Series | np.ndarray) -> StructureSnapshot | None:
    """Bull / chop / bear from MA20/60/120 alignment plus 120-day return.

    Bull requires the full stack *and* a positive 120-day return — the
    "completely judged bull" bar that voids the bearish prior. Bear is
    the mirror. Everything else is chop (the oscillation process).
    """
    values = np.asarray(close, dtype=float)
    if len(values) < MIN_BARS:
        return None
    last = float(values[-1])
    if not np.isfinite(last) or last <= 0:
        return None
    lagged = float(values[-1 - MOM_BARS])
    if not np.isfinite(lagged) or lagged <= 0:
        return None
    ma20 = _sma(values, MA_FAST)
    ma60 = _sma(values, MA_MID)
    ma120 = _sma(values, MA_SLOW)
    if not np.isfinite([ma20, ma60, ma120]).all():
        return None
    mom120 = last / lagged - 1.0
    if ma20 > ma60 > ma120 and last > ma60 and mom120 > 0:
        regime: Regime = "bull"
    elif ma20 < ma60 < ma120 and last < ma60 and mom120 < 0:
        regime = "bear"
    else:
        regime = "chop"
    return StructureSnapshot(
        regime=regime,
        close=last,
        ma20=ma20,
        ma60=ma60,
        ma120=ma120,
        mom120=mom120,
    )


def classify_horizons(close: pd.Series | np.ndarray) -> HorizonSnapshot | None:
    """Short / medium / long regimes from one CSI 300 close series.

    Medium is ``classify_structure`` unchanged — that is still the book gate.
    Short needs MA5/10/20. Long prefers MA250 when the tape is long enough;
    otherwise it uses close vs MA120 plus 120-day return.
    """
    structure = classify_structure(close)
    if structure is None:
        return None
    values = np.asarray(close, dtype=float)
    last = structure.close
    ma5 = _sma(values, MA_SHORT_FAST)
    ma10 = _sma(values, MA_SHORT_MID)
    if not np.isfinite([ma5, ma10]).all():
        return None
    ma250_raw = _sma(values, MA_YEAR) if len(values) >= MA_YEAR else float("nan")
    ma250 = float(ma250_raw) if np.isfinite(ma250_raw) else None
    short = _triple_stack_regime(last, ma5, ma10, structure.ma20)
    long = _long_regime(last, structure.ma120, ma250, structure.mom120)
    return HorizonSnapshot(
        short=short,
        medium=structure.regime,
        long=long,
        close=last,
        ma5=ma5,
        ma10=ma10,
        ma20=structure.ma20,
        ma60=structure.ma60,
        ma120=structure.ma120,
        ma250=ma250,
        mom120=structure.mom120,
        flip=_flip_hint(structure.regime, short, long),
    )


def _triple_stack_regime(last: float, fast: float, mid: float, slow: float) -> Regime:
    """Bull / bear only when price and three MAs agree in order; else chop."""
    if last > fast > mid > slow:
        return "bull"
    if last < fast < mid < slow:
        return "bear"
    return "chop"


def _long_regime(
    last: float,
    ma120: float,
    ma250: float | None,
    mom120: float,
) -> Regime:
    """Year-line guard. Below MA120 but above MA250 stays chop, not a bear market."""
    if ma250 is not None:
        if last > ma250 and last > ma120 and mom120 > 0:
            return "bull"
        if last < ma250 and last < ma120 and mom120 < 0:
            return "bear"
        return "chop"
    if last > ma120 and mom120 > 0:
        return "bull"
    if last < ma120 and mom120 < 0:
        return "bear"
    return "chop"


def _flip_hint(medium: Regime, short: Regime, long: Regime) -> str:
    """One-line Chinese: what the tape means, and what would change the book call."""
    if medium == "bear":
        if long != "bear":
            return (
                "中期偏空 = 今天先别买新的，不是熊市。"
                "年线还没破，已有仓按个股拿/减，不必一刀切清仓。"
                "转回来：收盘重新站上 60 日线。"
            )
        return (
            "中期+长期都偏空：今天不新买，0% 是上限，不是做空，也不是宣布熊市。"
            "转回来：收盘站上 60 日线（中期），再站上年线（长期）。"
            "经典熊市通常还要看从高点回撤是否到 20%。"
        )
    if medium == "bull":
        return "中期偏多对本主题票不加仓。近几天强弱只解释短线，不改这条。"
    if short == "bear":
        return (
            "只是近几天偏弱。仓位跟中期走：现在仍是震荡，低吸高抛，不是清仓。"
        )
    return (
        "中期震荡是常态。真正偏空要均线空排，收盘在 60 日线下，并且近半年收益为负。"
    )


def horizons_from_close(close: pd.Series | np.ndarray | None) -> dict[str, Any] | None:
    """JSON payload for pick/monitor boards. None when the tape is too short."""
    if close is None:
        return None
    snap = classify_horizons(close)
    return horizon_dict(snap)


def horizon_dict(snap: HorizonSnapshot | None) -> dict[str, Any] | None:
    """JSON-safe short/medium/long block. Legacy boards omit this key."""
    if snap is None:
        return None
    payload: dict[str, Any] = {
        "short": snap.short,
        "medium": snap.medium,
        "long": snap.long,
        "close": round(float(snap.close), 4),
        "ma5": round(float(snap.ma5), 4),
        "ma10": round(float(snap.ma10), 4),
        "ma20": round(float(snap.ma20), 4),
        "ma60": round(float(snap.ma60), 4),
        "ma120": round(float(snap.ma120), 4),
        "mom120": round(float(snap.mom120), 4),
        "flip": snap.flip,
    }
    if snap.ma250 is not None:
        payload["ma250"] = round(float(snap.ma250), 4)
    return payload


def classify_swing(close: pd.Series | np.ndarray, *, lookback: int = SWING_BARS) -> Swing | None:
    """Where today's close sits in the recent range (buy-low / sell-high)."""
    values = np.asarray(close, dtype=float)
    if len(values) < lookback:
        return None
    window = values[-lookback:]
    if not np.isfinite(window).all():
        return None
    lo = float(np.min(window))
    hi = float(np.max(window))
    last = float(window[-1])
    if hi <= lo:
        return "mid"
    loc = (last - lo) / (hi - lo)
    if loc <= OVERSOLD_MAX:
        return "oversold"
    if loc >= OVERBOUGHT_MIN:
        return "overbought"
    return "mid"


def long_only_plan(
    market: StructureSnapshot,
    stock: StructureSnapshot,
    swing: Swing,
) -> TrendPlan:
    """Map (index regime, stock regime, swing) onto a long-only action.

    Practice on this book's 2018–2026 tape inverted the usual "void fade
    in a bull market" rule:

    * CSI 300 bull + these names → ~26% 60-day up (do not chase).
    * CSI 300 chop + a name that itself is bull → ~52% 60-day up and a
      positive mean (relative strength, hold).
    * Chop + stock bear → ~17% 60-day up (cut / do not add).

    So the bearish prior stays on during index bull. It is relaxed only
    for names that print their own bull stack while the index is chop.
    Retail A-share accounts cannot short: bear never opens a short.
    """
    stock_reg = stock.regime
    mkt = market.regime

    if mkt == "bear":
        if swing == "overbought" or stock_reg == "bull":
            action: Action = "sell_rally"
            reason = "index bear: bounce is to sell, not a new uptrend"
        else:
            action = "wait"
            reason = "index bear: no new longs"
        return TrendPlan(mkt, stock_reg, swing, action, True, reason)

    if mkt == "bull":
        # Index bull did not lift this book. Keep fade-rallies on.
        if stock_reg == "bear":
            action = "reduce"
            reason = "index bull but this name is a bear stack — cut"
        elif swing == "overbought":
            action = "sell_rally"
            reason = "index bull: these names do not trend with CSI 300 — sell strength"
        else:
            action = "wait"
            reason = "index bull: do not add this book; wait for chop / a real dip process"
        return TrendPlan(mkt, stock_reg, swing, action, True, reason)

    # Index chop = the oscillation process (buy low, sell high, long only).
    if stock_reg == "bear":
        action = "reduce"
        reason = "index chop + stock bear: down-continuation is the reliable side — cut / do not add"
        return TrendPlan(mkt, stock_reg, swing, action, True, reason)
    if stock_reg == "bull":
        action = "hold"
        reason = "index chop + relative-strength bull: this is the cell that actually continued"
        return TrendPlan(mkt, stock_reg, swing, action, False, reason)
    if swing == "oversold":
        action = "buy_dip"
        reason = "index chop: buy the lower end of the range"
        return TrendPlan(mkt, stock_reg, swing, action, True, reason)
    if swing == "overbought":
        action = "sell_rally"
        reason = "index chop: sell the upper end of the range"
        return TrendPlan(mkt, stock_reg, swing, action, True, reason)
    action = "wait"
    reason = "index chop, mid-range: no add until price is cheap or the name turns bull"
    return TrendPlan(mkt, stock_reg, swing, action, True, reason)


def plan_from_frames(
    stock_close: pd.Series | np.ndarray,
    market_close: pd.Series | np.ndarray,
) -> TrendPlan | None:
    """Convenience: classify both series at their last bar and plan."""
    market = classify_structure(market_close)
    stock = classify_structure(stock_close)
    swing = classify_swing(stock_close)
    if market is None or stock is None or swing is None:
        return None
    return long_only_plan(market, stock, swing)


def snapshot_dict(snap: StructureSnapshot | None) -> dict[str, Any] | None:
    """JSON-safe structure snapshot for the forecast payload."""
    if snap is None:
        return None
    return {
        "regime": snap.regime,
        "close": round(float(snap.close), 4),
        "ma20": round(float(snap.ma20), 4),
        "ma60": round(float(snap.ma60), 4),
        "ma120": round(float(snap.ma120), 4),
        "mom120": round(float(snap.mom120), 4),
    }


@dataclass(frozen=True)
class PanelTrendSummary:
    """Per-board observation (RS/knife) plus index-gated action buckets.

    ``rs_n`` / ``knife_n`` are the stock's own MA stack (bull / bear). They
    stay populated when CSI 300 is bear and every *action* is wait/sell.
    ``dip_n`` / ``wait_n`` / ``sell_n`` still follow the long-only action.
    """

    classified: int = 0
    rs_n: int = 0
    dip_n: int = 0
    knife_n: int = 0
    wait_n: int = 0
    sell_n: int = 0

    @property
    def rs_share(self) -> float | None:
        if self.classified <= 0:
            return None
        return self.rs_n / self.classified

    @property
    def knife_share(self) -> float | None:
        if self.classified <= 0:
            return None
        return self.knife_n / self.classified


def add_plan_counts(plan: TrendPlan, counts: dict[str, int]) -> None:
    """Add one name to the panel tally.

    RS / knife follow ``stock_regime``, not the index-gated action. Index
    bear rewrites hold/reduce into wait/sell_rally; counting those actions
    as the observation map would print 0% / 0% on every board.
    """
    counts["classified"] += 1
    if plan.stock_regime == "bull":
        counts["rs_n"] += 1
    elif plan.stock_regime == "bear":
        counts["knife_n"] += 1
    if plan.action == "buy_dip":
        counts["dip_n"] += 1
    elif plan.action == "wait":
        counts["wait_n"] += 1
    elif plan.action == "sell_rally":
        counts["sell_n"] += 1


def summarize_panel(
    close: pd.DataFrame,
    market_close: pd.Series | np.ndarray,
) -> PanelTrendSummary:
    """Classify every column against one CSI 300 series; skip names that are too short."""
    if close is None or getattr(close, "empty", True):
        return PanelTrendSummary()
    counts = {
        "classified": 0,
        "rs_n": 0,
        "dip_n": 0,
        "knife_n": 0,
        "wait_n": 0,
        "sell_n": 0,
    }
    for col in close.columns:
        series = close[col]
        if series is None:
            continue
        plan = plan_from_frames(series.dropna(), market_close)
        if plan is None:
            continue
        add_plan_counts(plan, counts)
    return PanelTrendSummary(
        classified=counts["classified"],
        rs_n=counts["rs_n"],
        dip_n=counts["dip_n"],
        knife_n=counts["knife_n"],
        wait_n=counts["wait_n"],
        sell_n=counts["sell_n"],
    )


def trend_action_rank(action: str | None) -> int:
    """Lower is more actionable for a long-only book (hold before reduce)."""
    if action in ACTION_ORDER:
        return ACTION_ORDER.index(action)  # type: ignore[arg-type]
    return len(ACTION_ORDER)


def sort_dicts_by_trend_action(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable-ish sort: hold → buy_dip → sell_rally → wait → reduce, then weight."""

    def _key(item: dict[str, Any]) -> tuple[int, float, str]:
        trend = item.get("trend")
        action = trend.get("action") if isinstance(trend, dict) else None
        try:
            weight = -abs(float(item.get("weight") or 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        return (trend_action_rank(str(action) if action else None), weight, str(item.get("symbol") or ""))

    return sorted(items, key=_key)


def sort_holdings_by_trend(holdings: list[Any]) -> list[Any]:
    """Same action order for HoldingRow-like objects with optional ``trend`` dicts."""

    def _key(row: Any) -> tuple[int, float, str]:
        trend = getattr(row, "trend", None)
        action = trend.get("action") if isinstance(trend, dict) else None
        try:
            weight = -abs(float(getattr(row, "weight", 0.0) or 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        return (
            trend_action_rank(str(action) if action else None),
            weight,
            str(getattr(row, "symbol", "") or ""),
        )

    return sorted(holdings, key=_key)


def stamp_holding_trends(
    holdings: list[Any],
    data_map: dict[str, Any],
    market_close: pd.Series | np.ndarray,
) -> list[Any]:
    """Stamp optional ``trend`` on each row and sort hold → … → reduce.

    Fail-soft: a missing frame or a series shorter than MA120 leaves
    ``trend`` unset. Does not overwrite ``HoldingRow.action`` (enter/trim).
    """
    if not holdings:
        return holdings
    for row in holdings:
        symbol = getattr(row, "symbol", None)
        if not symbol:
            continue
        frame = data_map.get(symbol) if isinstance(data_map, dict) else None
        if frame is None or getattr(frame, "empty", True):
            continue
        columns = getattr(frame, "columns", [])
        if "close" not in columns:
            continue
        plan = plan_from_frames(frame["close"], market_close)
        if plan is not None:
            row.trend = plan.to_dict()
    return sort_holdings_by_trend(holdings)
