"""Live sector scoring: EW vs MA20, 20-day momentum, confirm coverage.

Matches the F0 process knobs (MA20 timing, C ≥ 40% CS) so the overlay does
not invent a new alpha. Historical excess is attached for display only.
A leftover strategy book (too few names / one name dominating) is a state
gate on top of that ranking — it does not change live_score.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from src.monitor.models import WEIGHT_EPSILON, HoldingRow
from src.sector_pick.models import SectorScore, Stance

logger = logging.getLogger(__name__)

MA_PERIOD = 20
MOM_LOOKBACK = 20
CONFIRM_Q = 0.40
SMOOTH_W = 5

# Strategy-state gate (not a new alpha). F0 is Top6; a leftover 1–2 name book
# is the turnover/risk trap, not a sector bet, and must not be recommended.
MIN_HOLDING_COUNT = 3
MAX_NAME_WEIGHT = 0.50

# Same IR fallbacks as the confirm-leg research scripts.
FALLBACK_IR: dict[str, float] = {
    "gtja191_164": 0.145,
    "gtja191_171": 0.155,
    "gtja191_054": 0.152,
    "gtja191_124": 0.12,
    "gtja191_178": 0.12,
    "gtja191_185": 0.12,
    "gtja191_070": 0.12,
    "gtja191_127": 0.12,
    "gtja191_156": 0.12,
    "gtja191_129": 0.12,
    "gtja191_111": 0.12,
    "gtja191_077": 0.12,
    "gtja191_100": 0.12,
    "gtja191_081": 0.121,
    "gtja191_160": 0.145,
}


def close_panel(data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Build a date × symbol close panel from per-symbol OHLCV frames."""
    cols = sorted(data_map)
    frames = []
    for code in cols:
        frame = data_map[code]
        if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
            continue
        series = frame["close"].copy()
        series.name = code
        frames.append(series)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, axis=1).sort_index()
    panel = panel.dropna(how="all")
    return panel


def timing_and_momentum(close: pd.DataFrame) -> dict[str, float | None]:
    """Equal-weight index vs MA20 and 20-day return on the last bar.

    ``timing_mult`` is 1.0 when EW ≥ MA20, else 0.5 — same as the F0 overlay.
    """
    empty = {
        "timing_mult": None,
        "ew_ma20_ratio": None,
        "ret_20d": None,
    }
    if close is None or close.empty:
        return empty
    ew = close.mean(axis=1, skipna=True).dropna()
    if ew.empty:
        return empty
    ma = ew.rolling(window=MA_PERIOD, min_periods=max(5, MA_PERIOD // 2)).mean()
    last_ew = float(ew.iloc[-1])
    last_ma = float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else None
    if last_ma is None or last_ma <= 0 or last_ew != last_ew:
        return empty
    timing_mult = 1.0 if last_ew >= last_ma else 0.5
    ratio = last_ew / last_ma
    ret_20d: float | None = None
    if len(ew) > MOM_LOOKBACK:
        prev = float(ew.iloc[-(MOM_LOOKBACK + 1)])
        if prev > 0:
            ret_20d = last_ew / prev - 1.0
    return {
        "timing_mult": timing_mult,
        "ew_ma20_ratio": ratio,
        "ret_20d": ret_20d,
    }


def smooth_cs(raw: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile, then 5-day mean (F0 scoring step)."""
    cs = raw.rank(axis=1, pct=True, ascending=True)
    return cs.rolling(window=SMOOTH_W, min_periods=3).mean()


def confirm_pass_rate(
    close: pd.DataFrame,
    confirm_ids: tuple[str, ...],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
) -> float | None:
    """Share of names with IR-weighted C ≥ 40th percentile on the last bar.

    All-missing confirm days pass everyone (rate = 1.0), matching F0.
    """
    if close is None or close.empty:
        return None
    if not confirm_ids:
        return 1.0

    index = close.index
    columns = close.columns
    conf_num = pd.DataFrame(0.0, index=index, columns=columns)
    conf_den = pd.DataFrame(0.0, index=index, columns=columns)
    active = 0
    for aid in confirm_ids:
        sm = scores.get(aid)
        if sm is None:
            continue
        aligned = sm.reindex(index=index, columns=columns)
        weight = max(float(irs.get(aid, FALLBACK_IR.get(aid, 0.10))), 0.0)
        valid = aligned.notna()
        conf_num = conf_num.add(aligned.fillna(0.0) * weight, fill_value=0.0)
        conf_den = conf_den.add(valid.astype(float) * weight, fill_value=0.0)
        active += 1
    if active == 0:
        return None

    conf = conf_num.divide(conf_den.replace(0.0, np.nan))
    last = conf.iloc[-1]
    if last.isna().all():
        return 1.0
    threshold = float(last.quantile(CONFIRM_Q))
    ok = last.ge(threshold)
    # Names with no confirm at all are excluded from the rate, not counted as fail.
    valid_last = last.notna()
    if int(valid_last.sum()) == 0:
        return 1.0
    return float(ok[valid_last].mean())


def load_confirm_irs() -> dict[str, float]:
    """Alive-factor IRs from the survival store, with research fallbacks."""
    irs = dict(FALLBACK_IR)
    try:
        from src.factors.survival_store import get_status_map

        alphas = get_status_map()
    except Exception as exc:  # noqa: BLE001 — scoring still works on fallbacks
        logger.debug("sector-pick survival IRs unavailable: %s", exc)
        return irs
    for aid, row in alphas.items():
        if not str(aid).startswith("gtja191_"):
            continue
        if not isinstance(row, dict) or row.get("category") != "alive":
            continue
        try:
            irs[str(aid)] = float(row.get("ir") or irs.get(aid, 0.10))
        except (TypeError, ValueError):
            continue
    return irs


def assign_live_scores(
    rows: list[SectorScore],
    *,
    market_regime: str | None = None,
) -> tuple[SectorScore | None, Stance]:
    """Fill ``live_score`` / ``rank`` and pick the winner.

    Timing bonus (1.0 vs 0.0) dominates the 0.5/0.5 momentum+confirm ranks, so
    any board still above MA20 beats every board below it — unless CSI 300 is
    a full bull, in which case that bonus is stripped (index bull is not a
    buy signal for this book). Chop adds a small RS/knife overlay that cannot
    override a 0/1 timing gap.
    """
    scored = [
        row
        for row in rows
        if row.enabled and row.error is None and row.ret_20d is not None and row.timing_mult is not None
    ]
    for row in rows:
        if row not in scored:
            row.live_score = None
            row.rank = None
    if not scored:
        return None, "none"

    ret_ranks = _rank_pct([row.ret_20d for row in scored])
    conf_filled = [
        row.confirm_pass_rate if row.confirm_pass_rate is not None else 0.5
        for row in scored
    ]
    conf_ranks = _rank_pct(conf_filled)
    for row, ret_r, conf_r in zip(scored, ret_ranks, conf_ranks):
        # Index bull: "also above MA20" is not a reason to size up this book.
        timing_bonus = 0.0 if market_regime == "bull" else (1.0 if (row.timing_mult or 0.0) >= 1.0 else 0.0)
        score = timing_bonus + 0.5 * float(ret_r) + 0.5 * float(conf_r)
        if market_regime == "chop":
            rs = float(row.rs_share or 0.0)
            knife = float(row.knife_share or 0.0)
            score += 0.25 * rs - 0.25 * knife
        row.live_score = score

    ranked = sorted(
        scored,
        key=lambda row: (
            float(row.live_score or 0.0),
            float(row.ret_20d or 0.0),
            float(row.confirm_pass_rate or 0.0),
            row.key,
        ),
        reverse=True,
    )
    for index, row in enumerate(ranked, start=1):
        row.rank = index
    winner = ranked[0]
    stance: Stance = "full" if (winner.timing_mult or 0.0) >= 1.0 else "half_watch"
    return winner, apply_regime_stance(stance, market_regime)


def apply_regime_stance(stance: Stance, market_regime: str | None) -> Stance:
    """Cap F0 stance: index bull never goes full; index bear recommends nothing."""
    if market_regime == "bear":
        return "none"
    if market_regime == "bull" and stance == "full":
        return "half_watch"
    return stance


def evaluate_book(holdings: list[HoldingRow] | None) -> tuple[int, float | None, str | None]:
    """Return (count, max_weight, skip_reason) for a strategy book.

    ``skip_reason`` is None when the book is tradable enough to recommend.
    """
    active = [
        row
        for row in (holdings or [])
        if abs(float(getattr(row, "weight", 0.0) or 0.0)) > WEIGHT_EPSILON
    ]
    count = len(active)
    max_weight = max((abs(float(row.weight)) for row in active), default=None)
    thin = count < MIN_HOLDING_COUNT
    concentrated = max_weight is not None and max_weight > MAX_NAME_WEIGHT
    if not thin and not concentrated:
        return count, max_weight, None
    bits = [f"持仓 {count} 只"]
    if max_weight is not None:
        bits.append(f"单票 {max_weight * 100:.0f}%")
    skip = (
        "策略残仓："
        + "，".join(bits)
        + f"（需至少 {MIN_HOLDING_COUNT} 只且单票≤{int(MAX_NAME_WEIGHT * 100)}%）"
    )
    return count, max_weight, skip


def stamp_book(row: SectorScore, holdings: list[HoldingRow] | None) -> bool:
    """Attach book diagnostics to ``row``. Returns True when it may be recommended."""
    count, max_weight, skip = evaluate_book(holdings)
    row.holding_count = count
    row.max_weight = max_weight
    row.skip_reason = skip
    row.book_healthy = skip is None
    return row.book_healthy is True


def first_tradable(ranked: list[SectorScore]) -> SectorScore | None:
    """First already-probed row whose book passed the holdings gate."""
    for row in ranked:
        if row.book_healthy is True:
            return row
    return None


def ranked_candidates(rows: list[SectorScore]) -> list[SectorScore]:
    """Enabled scored rows in live rank order (1 first)."""
    scored = [row for row in rows if row.rank is not None]
    return sorted(scored, key=lambda row: int(row.rank or 999))


def build_reason(
    winner: SectorScore | None,
    stance: Stance,
    *,
    index_leader: SectorScore | None = None,
    market_regime: str | None = None,
    horizons: dict | None = None,
) -> str:
    """One-line Chinese reason for the hero card (API returns this verbatim)."""
    prefix = _regime_reason_prefix(market_regime, horizons=horizons)
    if winner is None:
        return prefix + "没有可评分的板块：请勾选至少一条最优回测，或检查行情数据。"
    if stance == "none":
        # Index bear: still ranked for display, but do not size a new book.
        if market_regime == "bear":
            return prefix + "不推荐新开。"
        return prefix + "没有可评分的板块：请勾选至少一条最优回测，或检查行情数据。"
    bits: list[str] = []
    if stance == "full":
        bits.append("板块等权均价站上 MA20，按满仓择时")
    else:
        if market_regime == "bull":
            bits.append("指数多头不加仓，按半仓/观望")
        else:
            bits.append("全部勾选板块均低于 MA20，按半仓/观望")
    if winner.ret_20d is not None:
        bits.append(f"20日动量 {winner.ret_20d * 100:.1f}%")
    if winner.confirm_pass_rate is not None:
        bits.append(f"确认覆盖 {winner.confirm_pass_rate * 100:.0f}%")
    text = prefix + f"{winner.name}：" + "；".join(bits)
    if (
        index_leader is not None
        and index_leader.key != winner.key
        and index_leader.skip_reason
    ):
        # Index rank still uses EW/MA20; the gate only blocks leftover books.
        text = f"{text}。指数第一 {index_leader.name} 已跳过（{index_leader.skip_reason}）"
    return text


def build_untradable_reason(
    index_leader: SectorScore | None,
    *,
    market_regime: str | None = None,
    horizons: dict | None = None,
) -> str:
    """Hero copy when every probed book failed the holdings gate."""
    prefix = _regime_reason_prefix(market_regime, horizons=horizons)
    if index_leader is None:
        return prefix + "没有可评分的板块：请勾选至少一条最优回测，或检查行情数据。"
    skip = index_leader.skip_reason or "策略残仓"
    return (
        f"{prefix}没有可执行账本：指数领先的 {index_leader.name} 为{skip}；"
        "其余勾选板块持仓过少或单票过重，不作为首选。"
    )


def _regime_reason_prefix(market_regime: str | None, horizons: dict | None = None) -> str:
    """CSI 300 dual-track line; empty when the index bar was not classified."""
    if market_regime == "bull":
        return "沪深300中期偏多：这批主题票不加仓。"
    if market_regime == "bear":
        long_leg = ""
        if isinstance(horizons, dict):
            long_leg = str(horizons.get("long") or "").strip().lower()
        if long_leg and long_leg != "bear":
            return "沪深300中期偏空：今天先别买新的。年线未破，不是熊市清仓。"
        return "沪深300中期偏空：今天先别买新的。0% 是不新买的上限，不是一键清仓，也不是宣布熊市。"
    if market_regime == "chop":
        return "沪深300中期震荡：按个股低吸高抛。"
    return ""


def _rank_pct(values: list[float | None]) -> list[float]:
    """Higher value → closer to 1.0. A single name gets 1.0."""
    series = pd.Series(values, dtype="float64")
    if series.notna().sum() <= 1:
        return [1.0 if pd.notna(v) else 0.5 for v in values]
    ranked = series.rank(pct=True, ascending=True)
    return [float(v) if pd.notna(v) else 0.5 for v in ranked.tolist()]


def historical_metrics(run_dir: Any) -> tuple[float | None, float | None]:
    """Read excess/sharpe from ``artifacts/metrics.csv`` when present."""
    from pathlib import Path

    path = Path(run_dir) / "artifacts" / "metrics.csv"
    if not path.is_file():
        return None, None
    try:
        frame = pd.read_csv(path)
        if frame.empty:
            return None, None
        row = frame.iloc[0]
        excess = _cell_float(row, "excess_return")
        sharpe = _cell_float(row, "sharpe")
        return excess, sharpe
    except Exception as exc:  # noqa: BLE001
        logger.debug("sector-pick metrics unreadable for %s: %s", path, exc)
        return None, None


def _cell_float(row: Any, column: str) -> float | None:
    if column not in row.index:
        return None
    try:
        value = float(row[column])
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return value
