"""Design-regime inference for Monitor boards.

Purpose
-------
Every backtest bound to a Monitor board carries a ``strategy_style`` block whose
``designed_regime`` field answers one question: *what market regime was this
structure designed to work in?*  Filling it with ``unspecified`` made that
question unanswerable, so this module infers a value **from inputs that are
knowable before seeing the strategy's own P&L**:

1. ``mechanics.*`` — what the structure can and cannot survive, read straight
   from the declared style (sector timing, exit rules, rebalance cadence,
   breadth).  A 20-day time stop plus a 35% take profit is a buy-the-dip /
   sell-the-pop cadence; it mechanically clips winners in a fast tape.
2. ``panel.*`` — what the sector's own history looks like: how many days sit in
   each regime, how efficient the trend is, how often MA20 gets crossed.

Both channels are *about the design and the market*, not about the equity curve,
so the label cannot be reverse-engineered from a good or bad result.

The strategy's realised performance is computed afterwards and attached as
``post_hoc`` for disclosure only (``used_for_decision: false``).  It never
changes the verdict; it is there so a mismatch is visible instead of hidden.

Manual override
---------------
Auto-inference is a default, not an authority.  ``apply_override()`` writes a
human decision into the same config block with ``source="manual_override"``,
requires a non-empty reason, and keeps the auto value under ``previous_auto``
so the disagreement stays auditable.  ``designed_regime == "auto"`` resets.

Stdlib only: this module is imported by the API server.
"""

from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1
METHOD = "mechanics_x_panel_v1"

REGIMES: Tuple[str, ...] = ("bull", "slow_bull", "chop", "bear")

REGIME_LABELS_ZH: Dict[str, str] = {
    "bull": "快速单边多头",
    "slow_bull": "慢牛（温和上行）",
    "chop": "震荡",
    "bear": "空头",
}

# ---------------------------------------------------------------------------
# 机制 → 市况 匹配表
#
# Each row is a fixed, human-written prior: "a structure with this property is
# expected to earn its keep in these regimes".  The tables are constants on
# purpose — they ship inside the evidence payload so any verdict can be traced
# back to the exact numbers that produced it.
# ---------------------------------------------------------------------------

TIMING_SCORES: Dict[str, Dict[str, float]] = {
    # MA20 半仓择时：跌破板块等权 MA20 就砍半仓。趋势里砍掉的是收益，
    # 震荡/慢牛里砍掉的是回撤。
    "ma20_half": {"bull": 0.26, "slow_bull": 0.66, "chop": 0.80, "bear": 0.40},
    # 关闭择时 = 全程满仓暴露，只适合本身就走趋势的板块。
    "off": {"bull": 0.78, "slow_bull": 0.88, "chop": 0.50, "bear": 0.18},
    "unknown": {"bull": 0.50, "slow_bull": 0.58, "chop": 0.60, "bear": 0.35},
}

EXIT_SCORES: Dict[str, Dict[str, float]] = {
    # 35% 止盈 + 20 日时间止损 + 最短持有 5 日 = 低吸高抛、短持有。
    # 快牛里会把赢家提前卖飞，震荡里反复兑现正好。
    "low_buy_high_sell": {"bull": 0.32, "slow_bull": 0.70, "chop": 0.78, "bear": 0.38},
    # 有止盈但持有期长，偏向吃到一段趋势而非博反弹。
    "profit_target_long_hold": {"bull": 0.60, "slow_bull": 0.78, "chop": 0.52, "bear": 0.36},
    # 无止盈 = 让利润奔跑，趋势跟随特征。
    "trend_follow": {"bull": 0.82, "slow_bull": 0.72, "chop": 0.40, "bear": 0.34},
    "unknown": {"bull": 0.50, "slow_bull": 0.60, "chop": 0.60, "bear": 0.35},
}

REBALANCE_SCORES: Dict[str, Dict[str, float]] = {
    "high": {"bull": 0.50, "slow_bull": 0.60, "chop": 0.85, "bear": 0.30},
    "mid": {"bull": 0.42, "slow_bull": 0.74, "chop": 0.72, "bear": 0.38},
    "low": {"bull": 0.65, "slow_bull": 0.80, "chop": 0.45, "bear": 0.40},
    "unknown": {"bull": 0.50, "slow_bull": 0.60, "chop": 0.60, "bear": 0.35},
}

BREADTH_SCORES: Dict[str, Dict[str, float]] = {
    # 窄池 = 押板块 beta，方向性更重；宽池 = 截面分化，震荡里才有 alpha。
    "narrow": {"bull": 0.52, "slow_bull": 0.72, "chop": 0.55, "bear": 0.45},
    "mid": {"bull": 0.55, "slow_bull": 0.66, "chop": 0.75, "bear": 0.40},
    "wide": {"bull": 0.52, "slow_bull": 0.62, "chop": 0.82, "bear": 0.38},
    "very_wide": {"bull": 0.50, "slow_bull": 0.58, "chop": 0.88, "bear": 0.35},
}

WEIGHTS: Dict[str, float] = {
    "mechanics.timing": 0.26,
    "mechanics.exit_rules": 0.20,
    "mechanics.rebalance": 0.10,
    "mechanics.breadth": 0.12,
    "panel.regime_share": 0.20,
    "panel.trend_fit": 0.12,
}

# 熊市占比不足时，不允许把「空头」作为设计市况：长多结构在熊市只能防守，
# 不能算设计目标。
BEAR_MIN_SHARE = 0.40

# 两个候选的得分差小于此值时视为平局：不硬选一个，交回板块形态裁决。
TIE_MARGIN = 0.015

# 事后归因至少要有的交易日数（太短的片段均值没有意义）。
EMPIRICAL_MIN_DAYS = 30


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(var, 0.0))


def _rolling_mean(values: Sequence[float], window: int, idx: int) -> Optional[float]:
    if window <= 0 or idx + 1 < window:
        return None
    return sum(values[idx + 1 - window : idx + 1]) / window


def _isotime() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def overrides_path() -> Path:
    """Where manual overrides are journaled (outside any run dir)."""
    env = os.environ.get("VIBE_TRADING_REGIME_OVERRIDES")
    if env:
        return Path(env)
    return Path.home() / ".vibe-trading" / "regime_overrides.json"


# ---------------------------------------------------------------------------
# panel profile — what the sector itself looks like
# ---------------------------------------------------------------------------


def _load_close_returns(path: Path) -> Dict[str, float]:
    """daily simple returns keyed by trade_date."""
    out: Dict[str, float] = {}
    prev: Optional[float] = None
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                day = (row.get("trade_date") or row.get("date") or "").strip()
                close = _f(row.get("close"))
                if not day or close is None or close <= 0:
                    continue
                if prev is not None and prev > 0:
                    out[day] = close / prev - 1.0
                prev = close
    except OSError:
        return {}
    return out


def build_panel_index(artifacts_dir: Path) -> Tuple[List[str], List[float], int]:
    """Equal-weight index of every ``ohlcv_*.csv`` in the run artifacts.

    Returns ``(dates, values, n_symbols)`` with ``values[0] = 1.0``.  A stock
    enters the basket from its second bar (the first bar has no return), so
    staggered listings do not distort the early part of the curve.
    """
    per_symbol: List[Dict[str, float]] = []
    for path in sorted(Path(artifacts_dir).glob("ohlcv_*.csv")):
        rets = _load_close_returns(path)
        if len(rets) >= 30:
            per_symbol.append(rets)
    if not per_symbol:
        return [], [], 0

    day_rets: Dict[str, List[float]] = {}
    for rets in per_symbol:
        for day, r in rets.items():
            day_rets.setdefault(day, []).append(r)

    dates = sorted(day_rets)
    values: List[float] = []
    level = 1.0
    for day in dates:
        bucket = day_rets[day]
        if len(bucket) < 3:
            # too thin to call it a panel index; carry the level forward
            values.append(level)
            continue
        level *= 1.0 + (sum(bucket) / len(bucket))
        values.append(level)
    return dates, values, len(per_symbol)


def _trend_fit(values: Sequence[float]) -> Tuple[float, float]:
    """R² and annualised slope of log(level) regressed on time.

    R² is scale-free, so a 45%-vol sector and a 12%-vol sector stay comparable —
    unlike Kaufman's efficiency ratio, which collapses toward zero as soon as
    daily noise is large and therefore always votes "chop".
    """
    n = len(values)
    if n < 10:
        return 0.0, 0.0
    ys = [math.log(v) for v in values if v > 0]
    n = len(ys)
    if n < 10:
        return 0.0, 0.0
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((i - mx) * (ys[i] - my) for i in range(n))
    if sxx <= 0 or syy <= 0:
        return 0.0, 0.0
    r2 = (sxy ** 2) / (sxx * syy)
    slope = sxy / sxx
    return r2, slope * 252.0


def panel_profile(dates: Sequence[str], values: Sequence[float]) -> Dict[str, Any]:
    """Regime composition and trend character of the panel index."""
    n = len(values)
    if n < 60:
        return {"ok": False, "reason": "样本不足 60 个交易日，无法判定板块市况特征"}

    rets: List[float] = [0.0]
    for i in range(1, n):
        rets.append(values[i] / values[i - 1] - 1.0 if values[i - 1] > 0 else 0.0)

    ma_w = 120 if n >= 300 else 60 if n >= 160 else 20
    by_day = _regime_labels_by_day(dates, values, ma_w)
    labeled = list(by_day.values())
    counts = {r: 0 for r in REGIMES}
    for lb in labeled:
        counts[lb] += 1
    total = len(labeled) or 1
    shares = {r: counts[r] / total for r in REGIMES}

    # Kaufman efficiency ratio: |net move| / path travelled.  Kept as a
    # descriptive number only — it is not used for scoring because daily noise
    # dominates its denominator on high-vol panels.
    travelled = sum(abs(rets[i]) for i in range(1, n))
    efficiency = abs(values[-1] / values[0] - 1.0) / travelled if travelled > 0 else 0.0
    trend_r2, trend_slope = _trend_fit(values)

    ma20 = [_rolling_mean(values, 20, i) for i in range(n)]
    above20 = [i for i in range(n) if ma20[i] is not None and values[i] > ma20[i]]
    with20 = [i for i in range(n) if ma20[i] is not None]
    below20_ratio = 1.0 - (len(above20) / len(with20)) if with20 else 0.0
    crosses = 0
    prev_sign = None
    for i in with20:
        sign = 1 if values[i] > ma20[i] else -1
        if prev_sign is not None and sign != prev_sign:
            crosses += 1
        prev_sign = sign
    years = max(n / 252.0, 1e-9)

    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    daily = rets[1:]
    return {
        "ok": True,
        "_labels": by_day,
        "days": n,
        "labeled_days": len(labeled),
        "index_ma_window": ma_w,
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "window": [dates[0], dates[-1]] if dates else [None, None],
        "regime_share": {r: _round(shares[r], 3) for r in REGIMES},
        "trend_efficiency": _round(efficiency, 3),
        "trend_r2": _round(trend_r2, 3),
        "trend_slope_ann": _round(trend_slope, 3),
        "annual_vol": _round(_stdev(daily) * math.sqrt(252.0), 3),
        "panel_max_drawdown": _round(max_dd, 3),
        "annual_return": _round((values[-1] / values[0]) ** (252.0 / n) - 1.0, 3),
        "below_ma20_ratio": _round(below20_ratio, 3),
        "ma20_crosses_per_year": _round(crosses / years, 1),
    }


# ---------------------------------------------------------------------------
# mechanics priors — what the structure can survive
# ---------------------------------------------------------------------------


def _timing_key(style: Dict[str, Any]) -> str:
    timing = str(style.get("sector_timing") or "unknown").strip().lower()
    return timing if timing in TIMING_SCORES else "unknown"


def _exit_key(style: Dict[str, Any]) -> str:
    rules = style.get("risk_rules") or {}
    tp = _f(rules.get("take_profit"))
    time_stop = _f(rules.get("time_stop_days"))
    min_hold = _f(rules.get("min_hold_days"))
    if tp and tp > 0 and time_stop and time_stop <= 40 and (min_hold is None or min_hold <= 10):
        return "low_buy_high_sell"
    if tp and tp > 0 and (not time_stop or time_stop > 40):
        return "profit_target_long_hold"
    if not tp:
        return "trend_follow"
    return "unknown"


def _rebalance_key(style: Dict[str, Any]) -> str:
    freq = str(style.get("rebalance") or "unknown").strip().lower()
    rules = style.get("risk_rules") or {}
    cap = _f(rules.get("max_turnover_per_rebalance"))
    if freq in ("daily", "1d"):
        return "high"
    if freq in ("weekly", "biweekly", "2w"):
        # a weekly cadence with a hard cap behaves like a mid-frequency book
        return "mid" if (cap is None or cap <= 0.35) else "high"
    if freq in ("monthly", "quarterly", "1m", "3m"):
        return "low"
    return "unknown"


def _breadth_key(style: Dict[str, Any], fallback: Optional[int]) -> str:
    n = style.get("breadth")
    n_int = int(n) if isinstance(n, (int, float)) else fallback
    if n_int is None:
        return "unknown"
    if n_int < 30:
        return "narrow"
    if n_int < 80:
        return "mid"
    if n_int < 150:
        return "wide"
    return "very_wide"


def _trend_scores(r2: float, slope: float) -> Dict[str, float]:
    if r2 >= 0.80 and slope > 0:
        return {"bull": 0.90, "slow_bull": 0.68, "chop": 0.20, "bear": 0.22}
    if r2 >= 0.80:
        return {"bull": 0.20, "slow_bull": 0.25, "chop": 0.35, "bear": 0.80}
    if r2 >= 0.55 and slope > 0:
        return {"bull": 0.58, "slow_bull": 0.85, "chop": 0.45, "bear": 0.30}
    if r2 >= 0.55:
        return {"bull": 0.25, "slow_bull": 0.30, "chop": 0.50, "bear": 0.70}
    return {"bull": 0.30, "slow_bull": 0.42, "chop": 0.85, "bear": 0.45}


_TIMING_NOTE = {
    "ma20_half": "板块等权跌破 MA20 即降到半仓：趋势行情里被砍掉的是收益，震荡/慢牛里被砍掉的是回撤。",
    "off": "全程满仓、不做板块择时：只在板块自身就走趋势时占优，震荡里回撤全额承受。",
    "unknown": "未声明板块择时方式，按中性处理。",
}

_EXIT_NOTE = {
    "low_buy_high_sell": "35% 止盈 + 20 日时间止损 + 最短持有 5 日：买跌卖涨的短持有节奏，快牛里会提前卖飞赢家。",
    "profit_target_long_hold": "有止盈但持有期偏长，偏向吃一段趋势而非博反弹。",
    "trend_follow": "无止盈、让利润奔跑，属趋势跟随特征，震荡里容易被反复回吐。",
    "unknown": "离场规则未完整声明，按中性处理。",
}

_REBALANCE_NOTE = {
    "high": "高频调仓，靠短线来回吃差价，适合波动频繁的震荡市。",
    "mid": "周频 + 单次换手上限，中频节奏，震荡与慢牛都能承接。",
    "low": "低频调仓，仓位变化慢，只有趋势持续才跟得上。",
    "unknown": "调仓频率未声明，按中性处理。",
}

_BREADTH_NOTE = {
    "narrow": "成分股少，接近板块 beta 押注，方向性重于截面 alpha。",
    "mid": "中等宽度，截面选股有一定空间。",
    "wide": "宽度充足，截面分化明显，震荡市里 alpha 机会更多。",
    "very_wide": "池子很宽，收益更多来自截面分化而非板块方向，典型震荡友好结构。",
}


def mechanics_factors(style: Dict[str, Any], panel_symbols: int) -> List[Dict[str, Any]]:
    t_key = _timing_key(style)
    e_key = _exit_key(style)
    r_key = _rebalance_key(style)
    b_key = _breadth_key(style, panel_symbols or None)
    breadth_value = style.get("breadth") if isinstance(style.get("breadth"), (int, float)) else panel_symbols

    return [
        {
            "id": "mechanics.timing",
            "label": "板块择时",
            "weight": WEIGHTS["mechanics.timing"],
            "value": t_key,
            "scores": TIMING_SCORES[t_key],
            "note": _TIMING_NOTE[t_key],
        },
        {
            "id": "mechanics.exit_rules",
            "label": "离场规则",
            "weight": WEIGHTS["mechanics.exit_rules"],
            "value": e_key,
            "scores": EXIT_SCORES[e_key],
            "note": _EXIT_NOTE[e_key],
        },
        {
            "id": "mechanics.rebalance",
            "label": "调仓节奏",
            "weight": WEIGHTS["mechanics.rebalance"],
            "value": r_key,
            "scores": REBALANCE_SCORES[r_key],
            "note": _REBALANCE_NOTE[r_key],
        },
        {
            "id": "mechanics.breadth",
            "label": "池宽度",
            "weight": WEIGHTS["mechanics.breadth"],
            "value": f"{breadth_value} 只 / {b_key}",
            "scores": BREADTH_SCORES[b_key],
            "note": _BREADTH_NOTE[b_key],
        },
    ]


def panel_factors(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not profile.get("ok"):
        return []
    shares = profile["regime_share"]
    top_share = max(shares.values()) or 1.0
    share_scores = {
        r: _round(0.25 + 0.70 * (shares[r] / top_share), 3) for r in REGIMES
    }
    r2 = profile.get("trend_r2") or 0.0
    slope = profile.get("trend_slope_ann") or 0.0
    dom = max(shares, key=lambda r: shares[r])
    shape = (
        "强趋势向上" if r2 >= 0.80 and slope > 0
        else "强趋势向下" if r2 >= 0.80
        else "中等上行" if r2 >= 0.55 and slope > 0
        else "中等下行" if r2 >= 0.55
        else "无明确趋势"
    )
    return [
        {
            "id": "panel.regime_share",
            "label": "板块市况占比",
            "weight": WEIGHTS["panel.regime_share"],
            "value": "、".join(f"{REGIME_LABELS_ZH[r]} {shares[r] * 100:.0f}%" for r in REGIMES),
            "scores": share_scores,
            "note": (
                f"{profile['start']}~{profile['end']} 共 {profile['labeled_days']} 个可判定交易日，"
                f"以 {profile['index_ma_window']} 日均线状态划分，占比最高的是{REGIME_LABELS_ZH[dom]}。"
            ),
        },
        {
            "id": "panel.trend_fit",
            "label": "板块趋势形态",
            "weight": WEIGHTS["panel.trend_fit"],
            "value": f"R² {r2:.2f} / 年化斜率 {slope:+.1%}",
            "scores": _trend_scores(r2, slope),
            "note": (
                f"对数净值对时间回归：R² {r2:.2f}（≥0.80 强趋势、0.55–0.80 中等趋势、<0.55 无趋势），"
                f"年化斜率 {slope:+.1%} → {shape}；年化波动 {profile['annual_vol']:.0%}、"
                f"MA20 年均穿越 {profile['ma20_crosses_per_year']} 次。"
            ),
        },
    ]


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def _score(factors: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    total_w = sum(f["weight"] for f in factors) or 1.0
    out = {r: 0.0 for r in REGIMES}
    for f in factors:
        for r in REGIMES:
            out[r] += f["weight"] * float(f["scores"].get(r, 0.0))
    return {r: out[r] / total_w for r in REGIMES}


def infer_from_style(
    style: Dict[str, Any], profile: Dict[str, Any], panel_symbols: int
) -> Dict[str, Any]:
    """Pure scoring core — separated so tests need no filesystem."""
    factors = mechanics_factors(style, panel_symbols) + panel_factors(profile)
    raw = _score(factors)

    ranking = sorted(
        ({"regime": r, "score": _round(raw[r], 4)} for r in REGIMES),
        key=lambda x: x["score"],
        reverse=True,
    )

    bear_share = float((profile.get("regime_share") or {}).get("bear") or 0.0)
    excluded: List[Dict[str, str]] = []
    eligible = [row for row in ranking if row["regime"] != "bear"]
    bear_row = next((row for row in ranking if row["regime"] == "bear"), None)
    if bear_row and bear_share < BEAR_MIN_SHARE:
        excluded.append(
            {
                "regime": "bear",
                "reason": f"板块历史空头日占比 {bear_share:.0%} < {BEAR_MIN_SHARE:.0%}，长多结构不以熊市为设计目标",
            }
        )
    else:
        eligible = ranking

    top = eligible[0]
    second = eligible[1] if len(eligible) > 1 else {"regime": top["regime"], "score": 0.0}
    margin = float(top["score"]) - float(second["score"])

    # 机制层无法区分两个候选时（差值 < TIE_MARGIN），不硬选：改由板块形态裁决，
    # 并把"这是平局"这件事写进结论，避免让人误以为系统给出了明确判断。
    tie = margin < TIE_MARGIN and second["regime"] != top["regime"]
    tie_note = None
    if tie:
        shares = (profile.get("regime_share") or {})
        a, b = top["regime"], second["regime"]
        sa, sb = float(shares.get(a) or 0), float(shares.get(b) or 0)
        # 占比差距不到 2 个百分点时，板块也分不出高下 → 保留机制层得分更高者，
        # 免得出现「选了分更低那个」的观感。
        pick = b if (sb - sa) >= 0.02 else a
        tie_note = (
            f"{REGIME_LABELS_ZH[a]}与{REGIME_LABELS_ZH[b]}得分差仅 {margin:.3f}，机制层无法区分；"
            f"按板块形态取{REGIME_LABELS_ZH[pick]}"
            f"（{REGIME_LABELS_ZH[a]}占 {sa * 100:.0f}%、{REGIME_LABELS_ZH[b]}占 {sb * 100:.0f}%）"
        )
        if pick != top["regime"]:
            top, second = second, top
            margin = -margin

    confidence = 0.30 + 3.0 * abs(margin)
    notes: List[str] = []
    labeled_days = int(profile.get("labeled_days") or 0)
    if labeled_days and labeled_days < 200:
        confidence *= 0.85
        notes.append(f"样本仅 {labeled_days} 个可判定交易日，置信度已下调 15%")
    unknown_mech = [f["label"] for f in factors if f["weight"] and str(f["value"]).endswith("unknown")]
    if unknown_mech:
        confidence -= 0.05
        notes.append("机制字段缺失：" + "、".join(unknown_mech))
    confidence = max(0.25, min(0.92, confidence))
    conf_label = "高" if confidence >= 0.70 else "中" if confidence >= 0.45 else "低"
    if tie_note:
        notes.insert(0, tie_note)

    return {
        "factors": factors,
        "scores": {r: _round(raw[r], 4) for r in REGIMES},
        "ranking": eligible,
        "excluded": excluded,
        "verdict": {
            "designed_regime": top["regime"],
            "designed_regime_label": REGIME_LABELS_ZH[top["regime"]],
            "confidence": _round(confidence, 3),
            "confidence_label": conf_label,
            "margin": _round(abs(margin), 4),
            "tie": tie,
            "tie_candidates": [top["regime"], second["regime"]] if tie else [],
            "runner_up": second["regime"],
            "runner_up_score": second["score"],
        },
        "confidence_notes": notes,
        "weights": WEIGHTS,
    }


# ---------------------------------------------------------------------------
# post-hoc check — disclosure only
# ---------------------------------------------------------------------------


def empirical_check(run_dir: Path, profile: Dict[str, Any], verdict: str) -> Dict[str, Any]:
    """Segment the strategy's own daily returns by the panel's regime label.

    This answers "does the declared regime also happen to be where the strategy
    made money?" — it is NOT an input to the verdict.
    """
    equity_path = Path(run_dir) / "artifacts" / "equity.csv"
    if not equity_path.exists() or not profile.get("ok"):
        return {"used_for_decision": False, "available": False, "reason": "缺少 equity 产物或板块画像"}

    labels_by_day = profile.get("_labels") or {}
    buckets: Dict[str, List[float]] = {r: [] for r in REGIMES}
    try:
        with open(equity_path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                day = (row.get("timestamp") or row.get("trade_date") or "").strip()[:10]
                ret = _f(row.get("ret"))
                if not day or ret is None:
                    continue
                label = labels_by_day.get(day)
                if label:
                    buckets[label].append(ret)
    except OSError:
        return {"used_for_decision": False, "available": False, "reason": "equity 读取失败"}

    by_regime: Dict[str, Dict[str, Any]] = {}
    for r in REGIMES:
        rets = buckets[r]
        if len(rets) < EMPIRICAL_MIN_DAYS:
            continue
        mean = sum(rets) / len(rets)
        vol = _stdev(rets)
        sharpe = (mean / vol * math.sqrt(252.0)) if vol > 0 else 0.0
        compounded = 1.0
        for x in rets:
            compounded *= 1.0 + x
        by_regime[r] = {
            "days": len(rets),
            "avg_daily_bp": _round(mean * 10000.0, 2),
            "cum_return": _round(compounded - 1.0, 4),
            "ann_return": _round(compounded ** (252.0 / len(rets)) - 1.0, 4),
            "sharpe": _round(sharpe, 3),
        }

    # 按日均收益排序，不按夏普：MA20 半仓择时在市况不利时降到半仓甚至空仓，
    # 波动被压得很低，夏普会被抬到失真（空头区间曾出现 5 以上的夏普）。
    ranked = sorted(by_regime.items(), key=lambda kv: kv[1]["avg_daily_bp"], reverse=True)
    rank = next((i + 1 for i, (r, _) in enumerate(ranked) if r == verdict), None)
    best = ranked[0][0] if ranked else None
    consistent = bool(rank is not None and rank <= 2)
    if rank:
        b = by_regime[best]
        note = (
            f"该策略在{REGIME_LABELS_ZH[best]}的日子里日均收益最高"
            f"（{b['avg_daily_bp']:.0f} bp/日，累计 {b['cum_return'] * 100:.0f}%）；"
            f"申报市况排第 {rank}/{len(ranked)}"
        )
        if not consistent:
            note += "。与申报不一致——这是事实，但不改判申报值，只作复核线索"
    else:
        note = f"申报市况样本不足 {EMPIRICAL_MIN_DAYS} 日，无法核对"
    return {
        "used_for_decision": False,
        "available": True,
        "metric": "avg_daily_bp",
        "by_regime": by_regime,
        "best_regime": best,
        "best_label": REGIME_LABELS_ZH.get(best or "", None),
        "declared_rank": rank,
        "consistent": consistent,
        "note": note,
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _regime_labels_by_day(dates: Sequence[str], values: Sequence[float], ma_w: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    n = len(values)
    for i in range(n):
        ma = _rolling_mean(values, ma_w, i)
        if ma is None:
            continue
        ma_prev = _rolling_mean(values, ma_w, i - 20) if i - 20 >= 0 else None
        if ma_prev is None:
            continue
        slope_up = ma > ma_prev
        above = values[i] > ma
        if above and slope_up:
            label = "slow_bull"
            if i >= 60:
                r60 = (values[i] / values[i - 60]) ** (252.0 / 60.0) - 1.0
                prev = values[i - 60 : i + 1]
                rets60 = [prev[k] / prev[k - 1] - 1.0 for k in range(1, len(prev))]
                v60 = _stdev(rets60) * math.sqrt(252.0)
                if v60 > 0 and r60 > 1.2 * v60:
                    label = "bull"
            out[dates[i]] = label
        elif (not above) and (not slope_up):
            out[dates[i]] = "bear"
        else:
            out[dates[i]] = "chop"
    return out


DESIGN_SOURCES = {"run_id", "prop_file", "hypothesis", "ex_ante_doc", "design_doc"}


def resolve_effective(style: Dict[str, Any], auto_regime: str) -> Tuple[str, str]:
    """Precedence: manual override > ex-ante design source > auto inference.

    An auto label never overrules a regime someone declared *before* seeing
    results — that would be exactly the "tune the standard to the outcome"
    move the whole declaration exists to prevent.
    """
    if style.get("designed_regime_source") == "manual_override" and style.get("designed_regime") in REGIMES:
        return str(style["designed_regime"]), "manual_override"

    orig = style.get("designed_regime_original") or style.get("designed_regime")
    orig_src = style.get("designed_regime_original_source") or style.get("designed_regime_source")
    if orig_src in DESIGN_SOURCES and orig in REGIMES:
        return str(orig), str(orig_src)
    return auto_regime, "auto_inferred"


def coverage_check(profile: Dict[str, Any], regime: str) -> Dict[str, Any]:
    """Does the sample actually contain the regime being declared?"""
    shares = (profile or {}).get("regime_share") or {}
    share = float(shares.get(regime) or 0.0)
    dom = max(shares, key=lambda r: shares[r]) if shares else None
    ok = bool(shares) and share >= 0.25
    note = (
        f"申报市况在样本内占 {share * 100:.0f}%"
        + (f"，板块主市况为{REGIME_LABELS_ZH[dom]}（{shares[dom] * 100:.0f}%）" if dom else "")
    )
    if shares and not ok:
        note += "。该市况样本偏少，申报缺乏足够的样本支撑"
    return {
        "regime": regime,
        "share": _round(share, 3),
        "dominant_regime": dom,
        "dominant_label": REGIME_LABELS_ZH.get(dom or "", None),
        "ok": ok,
        "note": note,
    }


def prior_check(style: Dict[str, Any], auto_regime: str) -> Dict[str, Any]:
    """Compare the auto label with the pre-existing declaration, if any."""
    orig = style.get("designed_regime_original") or style.get("designed_regime")
    orig_src = style.get("designed_regime_original_source") or style.get("designed_regime_source")
    if orig not in REGIMES or orig_src not in DESIGN_SOURCES:
        return {"available": False, "note": "无事前设计申报可对照"}
    agree = orig == auto_regime
    return {
        "available": True,
        "value": orig,
        "label": REGIME_LABELS_ZH[orig],
        "source": orig_src,
        "agree": agree,
        "note": (
            f"事前申报（{orig_src}）为{REGIME_LABELS_ZH[orig]}，与自动推断一致"
            if agree
            else f"事前申报（{orig_src}）为{REGIME_LABELS_ZH[orig]}，与自动推断的"
            f"{REGIME_LABELS_ZH[auto_regime]}不一致；按优先级保留事前申报"
        ),
    }


def infer_run(run_dir: Path) -> Dict[str, Any]:
    """Compute the evidence payload for one run dir (no writes)."""
    run_dir = Path(run_dir)
    config_path = run_dir / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"config.json 不可读：{config_path}（{exc}）") from exc

    style = config.get("strategy_style") or {}
    dates, values, symbols = build_panel_index(run_dir / "artifacts")
    profile = panel_profile(dates, values)
    if profile.get("ok"):
        profile["panel_symbols"] = symbols

    core = infer_from_style(style, profile, symbols or len(config.get("codes") or []))
    clean_profile = {k: v for k, v in profile.items() if not k.startswith("_")}

    # 生效申报值：人工覆盖 > 事前设计来源 > 自动推断
    effective, effective_source = resolve_effective(style, core["verdict"]["designed_regime"])
    empirical = empirical_check(run_dir, profile, effective)
    coverage = coverage_check(clean_profile, effective)
    prior = prior_check(style, core["verdict"]["designed_regime"])

    breakdown = [
        {
            "regime": r,
            "score": core["scores"][r],
            "contribution": _round(
                sum(f["weight"] * float(f["scores"].get(r, 0.0)) for f in core["factors"]), 4
            ),
        }
        for r in REGIMES
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "method": METHOD,
        "generated_at": _isotime(),
        "run_id": run_dir.name,
        "inputs": {
            "panel_symbols": symbols,
            "panel_days": clean_profile.get("days"),
            "labeled_days": clean_profile.get("labeled_days"),
            "window": [clean_profile.get("start"), clean_profile.get("end")],
            "mechanics_source": "config.json#strategy_style",
            "panel_source": "artifacts/ohlcv_*.csv（等权合成板块指数）",
            "excluded_inputs": "策略自身收益/净值/回撤/胜率不参与判定，只进入 checks.empirical 供核对",
        },
        "panel": clean_profile,
        "factors": core["factors"],
        "weights": core["weights"],
        "scores": core["scores"],
        "breakdown": breakdown,
        "ranking": core["ranking"],
        "excluded": core["excluded"],
        "verdict": core["verdict"],
        "effective": {"designed_regime": effective, "label": REGIME_LABELS_ZH[effective], "source": effective_source},
        "checks": {"coverage": coverage, "empirical": empirical, "prior": prior},
        "confidence_notes": core["confidence_notes"],
    }


def _summary_note(evidence: Dict[str, Any]) -> str:
    v = evidence["verdict"]
    mech = [f for f in evidence["factors"] if f["id"].startswith("mechanics.")]
    p = evidence.get("panel") or {}
    bits: List[str] = []
    if mech:
        bits.append("机制层 " + "、".join(f"{f['label']}={f['value']}" for f in mech[:2]))
    shares = p.get("regime_share") or {}
    if shares:
        dom = max(shares, key=lambda r: shares[r])
        window = p.get("window") or [None, None]
        bits.append(
            f"板块层 {window[0]}~{window[1]} {REGIME_LABELS_ZH[dom]}日占 {shares[dom] * 100:.0f}%"
            f"、趋势拟合 R² {p.get('trend_r2')}（斜率 {p.get('trend_slope_ann')}）"
        )
    scores = evidence.get("scores") or {}
    if v.get("tie"):
        a, b = (v.get("tie_candidates") or [v["designed_regime"], v["runner_up"]])[:2]
        bits.append(
            f"{REGIME_LABELS_ZH[a]} {scores.get(a, 0):.3f} 与 {REGIME_LABELS_ZH[b]} {scores.get(b, 0):.3f} "
            f"得分接近（差 {v['margin']:.3f}），机制层无法区分，按板块形态取{REGIME_LABELS_ZH[v['designed_regime']]}，"
            f"置信 {v['confidence']:.2f}"
        )
    else:
        bits.append(
            f"综合得分 {scores.get(v['designed_regime'], 0):.3f}"
            f"（次优 {REGIME_LABELS_ZH[v['runner_up']]} {v['runner_up_score']:.3f}），置信 {v['confidence']:.2f}"
        )
    coverage = (evidence.get("checks") or {}).get("coverage") or {}
    if coverage and not coverage.get("ok", True):
        bits.append(coverage.get("note", ""))
    return "；".join(b for b in bits if b)


def write_strategy_style(
    run_dir: Path,
    evidence: Dict[str, Any],
    *,
    source: str,
    override: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist the verdict + evidence into config.json#strategy_style."""
    run_dir = Path(run_dir)
    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    style = dict(config.get("strategy_style") or {})

    if "designed_regime_original" not in style:
        style["designed_regime_original"] = style.get("designed_regime", "unspecified")
        style["designed_regime_original_source"] = style.get("designed_regime_source", "none")

    verdict = evidence["verdict"]
    effective = (evidence.get("effective") or {}).get("designed_regime") or verdict["designed_regime"]
    effective_source = (evidence.get("effective") or {}).get("source") or source
    style["designed_regime"] = effective
    style["designed_regime_label"] = REGIME_LABELS_ZH[effective]
    style["designed_regime_source"] = effective_source
    style["designed_regime_confidence"] = (
        verdict.get("confidence") if effective_source == "auto_inferred" else None
    )
    style["designed_regime_auto"] = verdict["designed_regime"]
    style["designed_regime_auto_confidence"] = verdict.get("confidence")
    style["designed_regime_method"] = evidence["method"]
    style["designed_regime_inferred_at"] = evidence["generated_at"]
    style["designed_regime_note"] = note or _summary_note(evidence)
    style["designed_regime_evidence"] = evidence
    style["designed_regime_override"] = override

    config["strategy_style"] = style
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return style


def apply_override(run_dir: Path, regime: Optional[str], reason: str, by: str = "user") -> Dict[str, Any]:
    """Manual adjustment. ``regime=None`` (or ``"auto"``) restores inference."""
    run_dir = Path(run_dir)
    reason = (reason or "").strip()
    if regime not in (None, "auto") and regime not in REGIMES:
        raise ValueError(f"未知市况：{regime}，可选 {list(REGIMES)} 或 auto")
    regime_normalized = None if regime in (None, "auto") else regime
    if regime_normalized and not reason:
        raise ValueError("手工调整必须填写理由")
    reason = reason or "恢复自动推断"

    evidence = infer_run(run_dir)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    style = dict(config.get("strategy_style") or {})
    auto_regime = evidence["verdict"]["designed_regime"]
    current = (evidence.get("effective") or {}).get("designed_regime")

    if regime in (None, "auto"):
        stripped = {
            k: v for k, v in style.items()
            if k not in ("designed_regime_override",)
        }
        stripped["designed_regime_source"] = stripped.get("designed_regime_original_source", "none")
        fallback, fallback_src = resolve_effective(stripped, auto_regime)
        evidence["effective"] = {
            "designed_regime": fallback,
            "label": REGIME_LABELS_ZH[fallback],
            "source": fallback_src,
        }
        style_out = write_strategy_style(
            run_dir,
            evidence,
            source=fallback_src,
            override=None,
            note=_summary_note(evidence) + "（已撤销手工调整）",
        )
        record = {"action": "reset", "value": fallback, "auto": auto_regime, "by": by, "at": _isotime(), "reason": reason}
    else:
        if regime == current and style.get("designed_regime_source") == "manual_override":
            raise ValueError(f"当前已是人工申报的{REGIME_LABELS_ZH[regime]}，无需重复调整")
        evidence["effective"] = {
            "designed_regime": regime,
            "label": REGIME_LABELS_ZH[regime],
            "source": "manual_override",
        }
        override = {
            "by": by,
            "at": _isotime(),
            "reason": reason,
            "previous_value": current,
            "previous_source": style.get("designed_regime_source"),
            "auto": auto_regime,
            "auto_confidence": evidence["verdict"]["confidence"],
        }
        style_out = write_strategy_style(
            run_dir,
            evidence,
            source="manual_override",
            override=override,
            note=f"人工申报为{REGIME_LABELS_ZH[regime]}（自动推断为{REGIME_LABELS_ZH[auto_regime]}）。理由：{reason}",
        )
        record = {"action": "override", "value": regime, "auto": auto_regime, "by": by, "at": _isotime(), "reason": reason}

    journal = overrides_path()
    payload: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "overrides": {}}
    if journal.exists():
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload.setdefault("overrides", {})
        except (OSError, json.JSONDecodeError):
            payload = {"schema_version": SCHEMA_VERSION, "overrides": {}}
    entry = payload["overrides"].get(run_dir.name) or {"run_id": run_dir.name, "history": []}
    entry.update({k: record[k] for k in record})
    entry.setdefault("history", []).append(record)
    payload["overrides"][run_dir.name] = entry
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return style_out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _all_board_runs(home: Optional[Path] = None) -> List[str]:
    boards = (home or Path.home() / ".vibe-trading") / "monitor" / "boards"
    out: List[str] = []
    if not boards.is_dir():
        return out
    for path in sorted(boards.glob("run_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_id = ((data.get("strategy") or {}).get("run_id")) or path.stem[4:]
        if run_id:
            out.append(str(run_id))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="推断并回填 strategy_style.designed_regime")
    parser.add_argument("--runs-root", default=None, help="回测 run 根目录，默认 agent/runs")
    parser.add_argument("--run", action="append", default=[], help="指定 run_id，可重复")
    parser.add_argument("--all-boards", action="store_true", help="处理所有已绑定看板的 run")
    parser.add_argument("--write", action="store_true", help="写回 config.json（默认只打印）")
    parser.add_argument("--json", action="store_true", help="输出完整证据 JSON")
    args = parser.parse_args(argv)

    runs_root = Path(args.runs_root) if args.runs_root else Path(__file__).resolve().parents[3] / "runs"
    if not runs_root.is_dir():
        # fallback: repo layout agent/runs
        runs_root = Path(__file__).resolve().parents[3] / "agent" / "runs"
    targets = list(args.run)
    if args.all_boards or not targets:
        targets = _all_board_runs()

    rows = []
    for run_id in targets:
        run_dir = runs_root / run_id
        if not (run_dir / "config.json").exists():
            print(f"[skip] {run_id}: 无 config.json")
            continue
        try:
            evidence = infer_run(run_dir)
        except RuntimeError as exc:
            print(f"[skip] {run_id}: {exc}")
            continue
        v = evidence["verdict"]
        eff = evidence["effective"]
        rows.append((run_id, v, evidence))
        if args.json:
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            continue
        print(
            f"{run_id:<58} auto={v['designed_regime']:<10} eff={eff['designed_regime']:<10}"
            f"({eff['source']}) conf={v['confidence']:.2f} margin={v['margin']:.3f}"
        )
        print(f"    {_summary_note(evidence)}")
        checks = evidence.get("checks") or {}
        emp = checks.get("empirical") or {}
        if emp.get("available"):
            print(f"    实测最佳：{emp.get('best_label')}（事后归因，不参与判定）｜ {emp.get('note')}")
        cov = checks.get("coverage") or {}
        if cov:
            print(f"    样本覆盖：{cov.get('note')}")
        prior = checks.get("prior") or {}
        if prior.get("available"):
            print(f"    事前对照：{prior.get('note')}")

    if args.write and not args.json:
        for run_id, _v, evidence in rows:
            write_strategy_style(runs_root / run_id, evidence, source="auto_inferred")
        print(f"\n已写回 {len(rows)} 个 run 的 config.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
