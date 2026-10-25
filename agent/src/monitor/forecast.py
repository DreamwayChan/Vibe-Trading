"""Holdings scenario forecast: kNN analog retrieval → 10-day up/down K paths.

Deterministic research overlay for the monitor board. It does **not** claim
point forecasts. Each holding's recent 20-day *shape* (z-scored returns),
realized volatility, volume/turnover activity, and MA stack are matched
against earlier causal windows. CSI 300's own 20-day state filters the
neighbor pool so stock analogs come from a similar index tape. Overlapping
copies of the same episode are dropped so votes come from independent
events; softmax weights then mix those neighbors into up/down
probabilities and 10-day OHLC paths. CSI 300 analog is also a *strong reverse*
veto: a stock call stands unless the index itself is a high-conviction,
lopsided, large-move barometer pointing the other way. A weak index reading
is not a vote.

A 10-day analog path is a *scenario band*, not a major-trend call. CSI 300
analog can reverse-veto a stock call. A separate long-only regime overlay
(buy-dip / sell-rally / hold / reduce / wait) is attached from MA20/60/120
structure — see :mod:`src.monitor.trend_regime`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from src.api.helpers import RUNS_DIR
from src.monitor.models import WEIGHT_EPSILON, HoldingRow, MonitorConfig
from src.monitor.scan import (
    MonitorScanError,
    _apply_same_day_overlay,
    _fetch_universe_ohlcv,
    _is_daily_interval,
    _run_source_interval,
)
from src.monitor.trend_regime import classify_structure, horizons_from_close, plan_from_frames, sort_dicts_by_trend_action

logger = logging.getLogger(__name__)

# Forecast fetch is independent of the monitor scan (scan stays at 400).
# ~800 calendar days ≈ two years of sessions after weekends/holidays.
FORECAST_LOOKBACK_DAYS = 800
LOOKBACK_DAYS = FORECAST_LOOKBACK_DAYS
# Shape of "how it has been trading" — daily returns, not raw prices.
PATTERN_WINDOW = 20
# Forward trading days to project (A-share weekdays; holidays ignored).
HORIZON = 10
# Independent neighbors after greedy de-overlap (relax toward min if history is thin).
MAX_NEIGHBORS = 24
MIN_NEIGHBORS = 6
# Stop adding neighbors once similarity falls off the nearest match.
# Filling out to 24 mediocre windows pulls the vote toward the 2-year mean.
SIM_FLOOR = 0.45
SIM_GAP_FROM_BEST = 0.15
# Directional call only when the vote is both lopsided and style-supported.
CONFIDENCE_CALL_MIN = 0.15
PROB_MARGIN_CALL_MIN = 0.15
STYLE_CALL_MIN = 0.40
# CSI 300 analog: only a strong barometer may veto a stock call.
# Weak / two-sided index readings leave the stock analog in charge.
MARKET_INDEX = "000300.SH"
MARKET_SKEW_MIN = 0.70
MARKET_CONFIDENCE_MIN = 0.20
MARKET_STYLE_MIN = 0.45
# |10-day analog return| of the favored side; a 1% drift is not "large impact".
MARKET_IMPACT_MIN = 0.03
# Kept so older saved reports / tests can still name the legacy abstain veto.
MARKET_VETO_ABSTAIN = "market_abstain"
MARKET_VETO_DISAGREE = "market_disagree"
# Kept as an alias so older tests/callers can still pass top_k=.
TOP_K = MAX_NEIGHBORS
# Last realized bars sent to the chart (MA20 still uses the fuller series).
HISTORY_BARS_FOR_CHART = 60
# Cap so one click does not pull a 50-name universe through the analog loop.
MAX_FORECAST_SYMBOLS = 12
# Combined similarity mix. Volume is a real input (turnover if present, else
# share volume); price-only callers keep the older 70/20/10 mix so tests and
# thin series without volume are unchanged.
SHAPE_WEIGHT = 0.55
VOL_WEIGHT = 0.15
VOLUME_SHAPE_WEIGHT = 0.15
MA_WEIGHT = 0.10
VOLUME_LEVEL_WEIGHT = 0.05
SHAPE_WEIGHT_PRICE = 0.70
VOL_WEIGHT_PRICE = 0.20
# Drop stock windows whose concurrent CSI 300 20d tape disagrees with today.
REGIME_FLAT_RETURN = 0.01
REGIME_VOL_MATCH_MIN = 0.50
# Softmax temperature on combined sim. Smaller → nearest neighbor dominates.
SOFTMAX_TAU = 0.12
# ATR window used to cap a single projected day's range.
ATR_PERIOD = 14
# Vol match uses log-std ratio; floor avoids log(0).
_VOL_EPS = 1e-8

FetchOhlcv = Callable[
    [list[str], str, str, str, str],
    tuple[dict[str, pd.DataFrame], dict[str, str]],
]


@dataclass(frozen=True)
class AnalogMatch:
    """One historical window whose 20-day state resembles today."""

    end_index: int
    similarity: float
    forward_return: float
    weight: float = 0.0
    shape_sim: float = 0.0
    vol_sim: float = 0.0
    ma_sim: float = 0.0
    volume_shape_sim: float = 1.0
    volume_level_sim: float = 1.0


@dataclass(frozen=True)
class AnalogSearchResult:
    """Selected neighbors plus the full scored pool (for path fill / style)."""

    neighbors: list[AnalogMatch]
    pool: list[AnalogMatch]
    all_similarities: np.ndarray


def active_holdings(rows: Iterable[HoldingRow]) -> list[HoldingRow]:
    """Keep names the strategy is still targeting (skip exits / zero weight)."""
    out: list[HoldingRow] = []
    for row in rows:
        if abs(float(row.weight)) <= WEIGHT_EPSILON:
            continue
        if str(row.action or "").strip().lower() == "exit":
            continue
        out.append(row)
    out.sort(key=lambda r: abs(float(r.weight)), reverse=True)
    return out


def limit_up_pct(symbol: str) -> float:
    """A-share daily limit used to clip synthesized closes.

    ChiNext (300/301), STAR (688/689) and Beijing (BJ) names use 20%;
    main-board names stay at 10%. Non A-share tickers also get 10% as a
    conservative bound so analog jumps cannot explode the path.
    """
    text = str(symbol or "").strip().upper()
    code = text.split(".", 1)[0]
    suffix = text.split(".", 1)[1] if "." in text else ""
    if suffix == "BJ" or code.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


def next_weekdays(last: date, count: int = HORIZON) -> list[str]:
    """ISO dates of the next ``count`` Mon–Fri sessions after ``last``."""
    out: list[str] = []
    cursor = last
    while len(out) < count:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
    return out


def _history_key(config: MonitorConfig) -> str:
    run_id = (config.strategy.run_id or "").strip()
    if run_id:
        return run_id
    shadow_id = (config.strategy.shadow_id or "").strip()
    if shadow_id:
        return f"shadow_{shadow_id}"
    raise MonitorScanError("no strategy run_id configured")


def _parse_as_of(raw: date | str | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise MonitorScanError(f"invalid as_of date: {text!r}") from exc


def _bar_date(label: Any) -> str:
    ts = pd.Timestamp(label)
    if pd.isna(ts):
        raise ValueError("invalid bar timestamp")
    return ts.date().isoformat()


def _clip_frame(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Drop bars after ``as_of`` so history replay cannot leak future closes."""
    if frame is None or frame.empty:
        return frame
    index = pd.to_datetime(frame.index)
    mask = index.date <= as_of
    return frame.loc[mask]


def _require_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    needed = ("open", "high", "low", "close")
    missing = [c for c in needed if c not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV missing columns: {missing}")
    clean = frame.copy()
    clean.index = pd.to_datetime(clean.index)
    clean = clean.sort_index()
    clean = clean[~clean.index.duplicated(keep="last")]
    for col in needed:
        clean[col] = pd.to_numeric(clean[col], errors="coerce")
    if "volume" in clean.columns:
        clean["volume"] = pd.to_numeric(clean["volume"], errors="coerce").fillna(0.0)
    else:
        clean["volume"] = 0.0
    clean = clean.dropna(subset=list(needed))
    return clean


def _sma(values: np.ndarray, period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window_sum = float(np.sum(values[:period]))
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += float(values[i]) - float(values[i - period])
        out[i] = window_sum / period
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ATR_PERIOD) -> float:
    n = len(close)
    if n == 0:
        return 0.0
    tr = np.empty(n, dtype=float)
    tr[0] = float(high[0] - low[0])
    for i in range(1, n):
        tr[i] = max(
            float(high[i] - low[i]),
            abs(float(high[i] - close[i - 1])),
            abs(float(low[i] - close[i - 1])),
        )
    if n < period:
        return float(np.mean(tr))
    return float(np.mean(tr[-period:]))


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denom < 1e-12:
        return 0.0
    return float(np.dot(left, right) / denom)


def _zscore(values: np.ndarray) -> np.ndarray:
    """Unitless window shape: mean 0, std 1. Flat windows stay zeros (cosine 0)."""
    arr = np.asarray(values, dtype=float)
    std = float(np.std(arr))
    if std < 1e-12:
        return np.zeros_like(arr)
    return (arr - float(np.mean(arr))) / std


def _vol_match(std_query: float, std_cand: float, eps: float = _VOL_EPS) -> float:
    """1 when 20-day return std matches; decays with log-std ratio."""
    return float(math.exp(-abs(math.log((std_query + eps) / (std_cand + eps)))))


def _sign_cmp(left: float | None, right: float | None) -> int:
    if left is None or right is None:
        return 0
    if math.isnan(left) or math.isnan(right):
        return 0
    diff = float(left) - float(right)
    scale = max(1.0, abs(float(right)))
    if abs(diff) < 1e-12 * scale:
        return 0
    return 1 if diff > 0 else -1


def _ma_signs(
    close: float | None,
    ma5: float | None,
    ma10: float | None,
    ma20: float | None,
) -> tuple[int, int, int]:
    """Close vs MA5, MA5 vs MA10, MA10 vs MA20 as {-1, 0, +1}."""
    return (
        _sign_cmp(close, ma5),
        _sign_cmp(ma5, ma10),
        _sign_cmp(ma10, ma20),
    )


def _ma_match(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    hamming = sum(int(a != b) for a, b in zip(left, right))
    return 1.0 - hamming / 3.0


def _ma_structure_label(signs: tuple[int, int, int]) -> str:
    if signs == (1, 1, 1):
        return "bull"
    if signs == (-1, -1, -1):
        return "bear"
    return "mixed"


def _combined_sim(
    shape: float,
    vol: float,
    ma: float,
    volume_shape: float = 1.0,
    volume_level: float = 1.0,
    *,
    use_volume: bool = False,
) -> float:
    """Weighted analog score. Volume terms are skipped when the series has no activity."""
    if use_volume:
        return (
            SHAPE_WEIGHT * shape
            + VOL_WEIGHT * vol
            + VOLUME_SHAPE_WEIGHT * volume_shape
            + MA_WEIGHT * ma
            + VOLUME_LEVEL_WEIGHT * volume_level
        )
    return SHAPE_WEIGHT_PRICE * shape + VOL_WEIGHT_PRICE * vol + MA_WEIGHT * ma


def _daily_returns(close: np.ndarray) -> np.ndarray:
    if len(close) < 2:
        return np.array([], dtype=float)
    prev = close[:-1]
    safe = np.where(np.abs(prev) < 1e-12, np.nan, prev)
    rets = (close[1:] - safe) / safe
    return np.nan_to_num(rets, nan=0.0, posinf=0.0, neginf=0.0)


def _softmax_weights(similarities: np.ndarray, tau: float = SOFTMAX_TAU) -> np.ndarray:
    """Nearer neighbors get exponentially more vote. Tau is in similarity units."""
    sims = np.asarray(similarities, dtype=float)
    if sims.size == 0:
        return sims
    safe_tau = max(float(tau), 1e-8)
    shifted = sims / safe_tau
    shifted = shifted - float(np.max(shifted))
    weights = np.exp(shifted)
    total = float(np.sum(weights))
    if total < 1e-15:
        return np.full(sims.size, 1.0 / sims.size)
    return weights / total


def _with_softmax_weights(matches: list[AnalogMatch]) -> list[AnalogMatch]:
    if not matches:
        return []
    weights = _softmax_weights(np.array([m.similarity for m in matches], dtype=float))
    return [replace(m, weight=float(w)) for m, w in zip(matches, weights)]


def _too_weak_for_vote(sim: float, best_sim: float) -> bool:
    """True when this window is a mediocre leftover, not a real analog."""
    if sim < SIM_FLOOR:
        return True
    return sim < float(best_sim) - SIM_GAP_FROM_BEST


def _greedy_nonoverlap(
    scored: list[AnalogMatch],
    *,
    window: int,
    max_k: int,
    min_k: int,
    dedupe: bool,
) -> list[AnalogMatch]:
    """Pick highest-sim windows whose centers are at least ``window/2`` apart.

    One trending episode otherwise floods Top-K with 1-day-shifted copies of
    the same 20-bar stretch. After the nearest match, skip windows that fall
    below the similarity floor or more than ``SIM_GAP_FROM_BEST`` behind —
    those dilute the vote toward the unconditional mean. If greedy yields
    fewer than ``min_k``, leftover (overlapping / weaker) candidates fill in
    so the vote is not empty.
    """
    if not scored:
        return []
    ordered = sorted(scored, key=lambda m: m.similarity, reverse=True)
    cap = max(1, int(max_k))
    floor = min(max(1, int(min_k)), cap)
    if not dedupe:
        return ordered[:cap]

    min_center_gap = float(window) / 2.0
    best_sim = float(ordered[0].similarity)
    selected: list[AnalogMatch] = []
    leftover: list[AnalogMatch] = []
    stop_weak = False
    for cand in ordered:
        if stop_weak or len(selected) >= cap:
            leftover.append(cand)
            continue
        far_enough = all(
            abs(cand.end_index - picked.end_index) >= min_center_gap for picked in selected
        )
        if not far_enough:
            leftover.append(cand)
            continue
        # Keep taking independent windows until we have min_k, even if they
        # are mediocre; after that, refuse the long tail of "sort of similar".
        if len(selected) >= floor and _too_weak_for_vote(cand.similarity, best_sim):
            leftover.append(cand)
            stop_weak = True
            continue
        selected.append(cand)

    if len(selected) < floor:
        for cand in leftover:
            selected.append(cand)
            if len(selected) >= floor:
                break
    return selected[:cap]


def _activity_series(frame: pd.DataFrame) -> np.ndarray | None:
    """Per-bar activity for analog matching: turnover if present, else volume.

    Turnover is preferred because share volume is not comparable across
    float changes. Returns None when the column is missing or mostly zero so
    price-only similarity is used instead of a flat (always-zero) z-score.
    """
    n = len(frame)
    if n == 0:
        return None
    min_positive = max(PATTERN_WINDOW, int(n * 0.3))
    for col in ("turnover", "turnover_rate", "volume"):
        if col not in frame.columns:
            continue
        raw = pd.to_numeric(frame[col], errors="coerce").to_numpy(dtype=float)
        raw = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
        raw = np.maximum(raw, 0.0)
        if int(np.sum(raw > 0)) < min_positive:
            continue
        # A flat series z-scores to zeros and would drag every cosine to 0.
        if float(np.std(np.log1p(raw))) < 1e-8:
            continue
        return raw
    return None


def _activity_level(activity: np.ndarray, end: int, period: int = 20) -> float:
    """Last-bar activity vs its trailing mean (volume/turnover ratio)."""
    start = end - period + 1
    if start < 0 or end >= len(activity):
        return 1.0
    sl = activity[start : end + 1]
    mean = float(np.mean(sl))
    if mean <= _VOL_EPS:
        return 1.0
    return float(sl[-1] / mean)


def _index_close_series(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce")
    series = pd.Series(close.to_numpy(dtype=float), index=pd.to_datetime(frame.index))
    series = series.sort_index()
    series = series[~series.index.duplicated(keep="last")].dropna()
    return series


def _index_window_stats(
    index_close: pd.Series,
    as_of: date,
    window: int,
) -> tuple[float, float] | None:
    """CSI 300 20-session return and vol ending on or before ``as_of``."""
    if index_close.empty:
        return None
    as_of_ts = pd.Timestamp(as_of)
    sl = index_close.loc[index_close.index <= as_of_ts]
    if len(sl) < window + 1:
        return None
    closes = sl.to_numpy(dtype=float)[-(window + 1) :]
    prev = closes[:-1]
    safe = np.where(np.abs(prev) < 1e-12, np.nan, prev)
    rets = np.nan_to_num((closes[1:] - safe) / safe, nan=0.0, posinf=0.0, neginf=0.0)
    total = float(np.prod(1.0 + rets) - 1.0)
    vol = float(np.std(rets))
    return total, vol


def _regime_compatible(
    query: tuple[float, float],
    candidate: tuple[float, float],
) -> bool:
    """True when the index 20d sign (unless flat) and vol ballpark both match."""
    q_ret, q_vol = query
    c_ret, c_vol = candidate
    if _vol_match(q_vol, c_vol) < REGIME_VOL_MATCH_MIN:
        return False
    if abs(q_ret) < REGIME_FLAT_RETURN or abs(c_ret) < REGIME_FLAT_RETURN:
        return True
    return (q_ret > 0.0) == (c_ret > 0.0)


def _regime_ok_mask(
    stock_index: pd.DatetimeIndex,
    returns_len: int,
    *,
    window: int,
    horizon: int,
    index_frame: pd.DataFrame | None,
) -> np.ndarray | None:
    """Boolean mask over return-space ``end`` indices. None = do not filter."""
    if index_frame is None or getattr(index_frame, "empty", True):
        return None
    if "close" not in index_frame.columns:
        return None
    idx_close = _index_close_series(index_frame)
    last_day = pd.Timestamp(stock_index[-1]).date()
    query_stats = _index_window_stats(idx_close, last_day, window)
    if query_stats is None:
        return None
    mask = np.ones(returns_len, dtype=bool)
    last_end = returns_len - window - horizon
    for end in range(window, last_end + 1):
        if end >= len(stock_index):
            continue
        as_of = pd.Timestamp(stock_index[end]).date()
        stats = _index_window_stats(idx_close, as_of, window)
        if stats is None:
            continue
        mask[end] = _regime_compatible(query_stats, stats)
    return mask


def search_analogs(
    returns: np.ndarray,
    *,
    close: np.ndarray | None = None,
    ma5: list[float | None] | None = None,
    ma10: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    activity: np.ndarray | None = None,
    regime_ok: np.ndarray | None = None,
    window: int = PATTERN_WINDOW,
    horizon: int = HORIZON,
    max_k: int = MAX_NEIGHBORS,
    min_k: int = MIN_NEIGHBORS,
    dedupe: bool = True,
) -> AnalogSearchResult:
    """Causal kNN: z-scored shape + vol + optional volume + MA, then de-overlap.

    Candidate windows must finish before the query starts, and still have
    ``horizon`` subsequent returns in-sample. ``close``/MA are optional so
    return-only tests can exercise shape+vol without building OHLCV.
    ``regime_ok`` drops windows whose concurrent index tape disagrees with
    today; if that would leave fewer than ``min_k`` candidates the filter is
    skipped so a thin history still produces a band.
    """
    empty = AnalogSearchResult(neighbors=[], pool=[], all_similarities=np.array([], dtype=float))
    n = len(returns)
    if n < window * 2 + horizon:
        return empty

    query = returns[-window:]
    z_query = _zscore(query)
    std_query = float(np.std(query))
    query_ma = (0, 0, 0)
    use_ma = close is not None and ma5 is not None and ma10 is not None and ma20 is not None
    if use_ma:
        assert close is not None and ma5 is not None and ma10 is not None and ma20 is not None
        last_i = len(close) - 1
        query_ma = _ma_signs(float(close[last_i]), ma5[last_i], ma10[last_i], ma20[last_i])

    use_volume = (
        activity is not None
        and close is not None
        and len(activity) == len(close)
        and len(activity) >= window
    )
    log_activity = None
    query_vol_shape = None
    query_vol_level = 1.0
    activity_arr: np.ndarray | None = None
    if use_volume:
        assert activity is not None and close is not None
        activity_arr = activity
        log_activity = np.log1p(np.maximum(activity_arr, 0.0))
        query_vol_shape = _zscore(log_activity[-window:])
        query_vol_level = _activity_level(activity_arr, len(close) - 1, window)

    last_end = n - window - horizon
    ends = list(range(window, last_end + 1))
    if regime_ok is not None and len(regime_ok) >= n:
        filtered = [end for end in ends if bool(regime_ok[end])]
        # Too few same-tape windows: keep the unfiltered pool rather than empty.
        if len(filtered) >= int(min_k):
            ends = filtered

    scored: list[AnalogMatch] = []
    for end in ends:
        candidate = returns[end - window : end]
        shape = _cosine(z_query, _zscore(candidate))
        vol = _vol_match(std_query, float(np.std(candidate)))
        if use_ma:
            ma_sim = _ma_match(
                query_ma,
                _ma_signs(float(close[end]), ma5[end], ma10[end], ma20[end]),
            )
        else:
            ma_sim = 1.0
        volume_shape = 1.0
        volume_level = 1.0
        if (
            use_volume
            and activity_arr is not None
            and log_activity is not None
            and query_vol_shape is not None
        ):
            vol_slice = log_activity[end - window + 1 : end + 1]
            if len(vol_slice) == window:
                volume_shape = _cosine(query_vol_shape, _zscore(vol_slice))
            volume_level = _vol_match(
                query_vol_level, _activity_level(activity_arr, end, window)
            )
        sim = _combined_sim(
            shape,
            vol,
            ma_sim,
            volume_shape,
            volume_level,
            use_volume=use_volume,
        )
        fwd = returns[end : end + horizon]
        forward_return = float(np.prod(1.0 + fwd) - 1.0)
        scored.append(
            AnalogMatch(
                end_index=end,
                similarity=sim,
                forward_return=forward_return,
                shape_sim=shape,
                vol_sim=vol,
                ma_sim=ma_sim,
                volume_shape_sim=volume_shape,
                volume_level_sim=volume_level,
            )
        )

    selected = _greedy_nonoverlap(
        scored, window=window, max_k=max_k, min_k=min_k, dedupe=dedupe
    )
    neighbors = _with_softmax_weights(selected)
    all_sims = np.array([m.similarity for m in scored], dtype=float)
    return AnalogSearchResult(neighbors=neighbors, pool=scored, all_similarities=all_sims)


def find_analogs(
    returns: np.ndarray,
    *,
    close: np.ndarray | None = None,
    ma5: list[float | None] | None = None,
    ma10: list[float | None] | None = None,
    ma20: list[float | None] | None = None,
    activity: np.ndarray | None = None,
    regime_ok: np.ndarray | None = None,
    window: int = PATTERN_WINDOW,
    horizon: int = HORIZON,
    top_k: int = MAX_NEIGHBORS,
    min_k: int = MIN_NEIGHBORS,
    dedupe: bool = True,
) -> list[AnalogMatch]:
    """Nearest independent analog windows. Wrapper around :func:`search_analogs`."""
    result = search_analogs(
        returns,
        close=close,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        activity=activity,
        regime_ok=regime_ok,
        window=window,
        horizon=horizon,
        max_k=top_k,
        min_k=min_k,
        dedupe=dedupe,
    )
    return result.neighbors


def _style_bucket(score: float) -> str:
    if score >= 0.65:
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


def _style_score(neighbors: list[AnalogMatch], all_similarities: np.ndarray) -> float:
    """Mean selected sim (clipped to [0, 1]) × best-sim percentile among all windows.

    A high percentile of a still-distant nearest neighbor stays low because
    the absolute mean sim is small. That is the "history does not rhyme" case.
    """
    if not neighbors or all_similarities.size == 0:
        return 0.0
    avg = float(np.mean([m.similarity for m in neighbors]))
    avg_mapped = min(1.0, max(0.0, avg))
    best = max(m.similarity for m in neighbors)
    percentile = float(np.mean(all_similarities <= best))
    return min(1.0, max(0.0, avg_mapped * percentile))


def _confidence(up_prob: float, style_score: float) -> float:
    """How lopsided the vote is, discounted when analogs themselves are weak."""
    conviction = abs(float(up_prob) - 0.5) * 2.0
    return min(1.0, max(0.0, conviction * float(style_score)))


def directional_bias(
    up_prob: float,
    confidence: float,
    style_score: float = 1.0,
) -> tuple[str, bool]:
    """Return (bias, called). Abstain unless the vote is lopsided *and* confident.

    A 56% analog vote with style ~0.5 is a 2-year bullish prior, not a call.
    Low style (history does not rhyme) also withholds the label even if the
    few neighbors happen to share a direction. Both dashed K paths still render.
    """
    margin = abs(float(up_prob) - 0.5)
    if float(style_score) < STYLE_CALL_MIN:
        return "neutral", False
    if float(confidence) < CONFIDENCE_CALL_MIN or margin < PROB_MARGIN_CALL_MIN:
        return "neutral", False
    if float(up_prob) > 0.5:
        return "up", True
    return "down", True


def _path_return(bars: list[dict[str, Any]] | None, last_close: float) -> float:
    """Close-to-close return of a projected analog path, or 0 if unusable."""
    if not bars or last_close <= 0:
        return 0.0
    end = bars[-1].get("close")
    if end is None:
        return 0.0
    try:
        return float(end) / float(last_close) - 1.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def market_is_decisive(
    up_prob: float,
    confidence: float,
    style_score: float,
    impact: float,
) -> tuple[str, bool]:
    """True only when CSI 300 is a one-sided, high-confidence, large-move analog.

    A 51–67% index lean is a coin-flip barometer and must not silence stocks.
    ``impact`` is the favored side's 10-day analog return (signed).
    """
    prob = float(up_prob)
    skew = max(prob, 1.0 - prob)
    if float(style_score) < MARKET_STYLE_MIN:
        return "neutral", False
    if float(confidence) < MARKET_CONFIDENCE_MIN:
        return "neutral", False
    if skew < MARKET_SKEW_MIN:
        return "neutral", False
    if abs(float(impact)) < MARKET_IMPACT_MIN:
        return "neutral", False
    return ("up" if prob > 0.5 else "down"), True


def apply_market_veto(assessment: dict[str, Any], market: dict[str, Any] | None) -> dict[str, Any]:
    """Withhold a stock call only when CSI 300 is a strong opposite barometer.

    The index is a reverse gate, not an AND-confirm: weak or missing index
    readings leave the stock analog in charge. Paths never change; only
    ``called`` / ``bias`` are cleared on a strong disagreement.
    """
    out = dict(assessment)
    out.setdefault("market_veto", None)
    if not out.get("called"):
        return out
    if not market or market.get("error"):
        return out
    # Idle barometer: do not treat "index abstained" as a veto.
    if not market.get("decisive"):
        return out
    stock_bias = str(out.get("bias") or "")
    market_bias = str(market.get("bias") or "")
    if stock_bias in ("up", "down") and market_bias in ("up", "down") and market_bias != stock_bias:
        out["called"] = False
        out["bias"] = "neutral"
        out["market_veto"] = MARKET_VETO_DISAGREE
    return out


def _market_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Compact CSI 300 analog for the report header (not a holding card)."""
    assessment = item.get("assessment") or {}
    last_close = float(item.get("price") or 0.0)
    up_prob = float(item.get("up_prob") or 0.5)
    # Impact is the analog 10-day move of the side the vote currently favors.
    favored = _path_return(
        item.get("up_bars") if up_prob >= 0.5 else item.get("down_bars"),
        last_close,
    )
    bias, decisive = market_is_decisive(
        up_prob,
        float(assessment.get("confidence") or 0.0),
        float(item.get("style_score") or 0.0),
        favored,
    )
    return {
        "symbol": str(item.get("symbol") or MARKET_INDEX),
        "up_prob": item.get("up_prob"),
        "down_prob": item.get("down_prob"),
        "style_score": item.get("style_score"),
        "confidence": assessment.get("confidence"),
        "bias": bias,
        "called": decisive,
        "decisive": decisive,
        "impact": round(float(favored), 4),
        "analog_count": assessment.get("analog_count") or 0,
        "error": item.get("error"),
    }


def _weighted_mean(samples: list[float], weights: list[float], fallback: float) -> float:
    if not samples:
        return fallback
    w = np.asarray(weights, dtype=float)
    total = float(np.sum(w))
    if total < 1e-15:
        w = np.ones_like(w)
    return float(np.average(np.asarray(samples, dtype=float), weights=w))


def _geometry_from_analogs(
    frame: pd.DataFrame,
    matches: list[AnalogMatch],
    horizon: int = HORIZON,
) -> dict[str, list[float]]:
    """Similarity-weighted mean gap / return / wick fractions per forward day."""
    close = frame["close"].to_numpy(dtype=float)
    open_ = frame["open"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    gaps = [[] for _ in range(horizon)]
    rets = [[] for _ in range(horizon)]
    uppers = [[] for _ in range(horizon)]
    lowers = [[] for _ in range(horizon)]
    gap_w = [[] for _ in range(horizon)]
    ret_w = [[] for _ in range(horizon)]
    upper_w = [[] for _ in range(horizon)]
    lower_w = [[] for _ in range(horizon)]
    for match in matches:
        # Analog end_index is in *return* space; the matching close sits at end_index
        # (returns[i] uses close[i+1]/close[i], so analog window ends at close[end_index]).
        base = match.end_index
        weight = float(match.weight) if match.weight > 0 else 1.0
        for t in range(horizon):
            prev_i = base + t
            cur_i = base + t + 1
            if cur_i >= len(close) or prev_i < 0:
                continue
            prev_c = float(close[prev_i])
            if abs(prev_c) < 1e-12:
                continue
            o = float(open_[cur_i])
            h = float(high[cur_i])
            l = float(low[cur_i])
            c = float(close[cur_i])
            gaps[t].append(o / prev_c - 1.0)
            rets[t].append(c / prev_c - 1.0)
            body_top = max(o, c)
            body_bot = min(o, c)
            uppers[t].append(max(0.0, (h - body_top) / prev_c))
            lowers[t].append(max(0.0, (body_bot - l) / prev_c))
            gap_w[t].append(weight)
            ret_w[t].append(weight)
            upper_w[t].append(weight)
            lower_w[t].append(weight)
    return {
        "gap": [_weighted_mean(gaps[t], gap_w[t], 0.0) for t in range(horizon)],
        "ret": [_weighted_mean(rets[t], ret_w[t], 0.0) for t in range(horizon)],
        "upper": [_weighted_mean(uppers[t], upper_w[t], 0.002) for t in range(horizon)],
        "lower": [_weighted_mean(lowers[t], lower_w[t], 0.002) for t in range(horizon)],
    }


def _supplement_cluster(
    cluster: list[AnalogMatch],
    pool: list[AnalogMatch],
    *,
    positive: bool,
    min_n: int = 3,
) -> list[AnalogMatch]:
    """If a direction has <3 selected neighbors, borrow that side's outer half.

    Up paths use positive-forward windows at/above the median; down paths use
    negative-forward windows at/below the median. Avoids a cartoon ATR line
    when the vote is lopsided but the empty side still needs a drawable path.
    """
    if len(cluster) >= min_n:
        return cluster
    side = [
        m
        for m in pool
        if (m.forward_return > 0) is positive and m.forward_return != 0.0
    ]
    if not side:
        return cluster
    median_fwd = float(np.median(np.array([m.forward_return for m in side], dtype=float)))
    if positive:
        extras = [m for m in side if m.forward_return >= median_fwd]
    else:
        extras = [m for m in side if m.forward_return <= median_fwd]
    seen = {m.end_index for m in cluster}
    out = list(cluster)
    for match in sorted(extras, key=lambda m: m.similarity, reverse=True):
        if match.end_index in seen:
            continue
        out.append(match)
        seen.add(match.end_index)
        if len(out) >= min_n:
            break
    return out


def _trend_for_item(
    stock_frame: pd.DataFrame | None,
    market_frame: pd.DataFrame | None,
) -> dict[str, Any] | None:
    """Long-only major-trend plan; None when either series is too short."""
    if stock_frame is None or market_frame is None:
        return None
    try:
        stock = _require_ohlcv(stock_frame)
        market = _require_ohlcv(market_frame)
    except (ValueError, TypeError):
        return None
    plan = plan_from_frames(stock["close"], market["close"])
    return plan.to_dict() if plan else None


def _trend_rule(market_regime: str | None, horizons: dict[str, Any] | None = None) -> str:
    """One-line book rule for the forecast header."""
    long_leg = ""
    if isinstance(horizons, dict):
        long_leg = str(horizons.get("long") or "").strip().lower()
    if market_regime == "bull":
        return (
            "index medium bull: do not add this book; the 10-day analog is only a "
            "short-horizon reverse veto"
        )
    if market_regime == "bear":
        if long_leg and long_leg != "bear":
            return (
                "index medium soft-bear, year line intact: no new buys; "
                "keep/trim existing; not a bear-market flatten"
            )
        return (
            "index medium soft-bear: no new buys today; 0% is the cap, "
            "not a short and not a bear-market call"
        )
    if market_regime == "chop":
        return "index chop: buy dips, sell range highs; hold a name that itself is bull"
    return "major-trend tape too short to classify"


def _market_structure(frame: pd.DataFrame | None) -> dict[str, Any] | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        close = _require_ohlcv(frame)["close"]
    except (ValueError, TypeError):
        return None
    snap = classify_structure(close)
    if snap is None:
        return None
    out: dict[str, Any] = {"regime": snap.regime, "mom120": round(float(snap.mom120), 4)}
    horizons = horizons_from_close(close)
    if horizons:
        out["horizons"] = horizons
    return out


def _empty_item(symbol: str, weight: float, error: str) -> dict[str, Any]:
    """Stub row so the UI can show a fetch/data failure next to successful names."""
    item = forecast_symbol(symbol, pd.DataFrame({"open": [], "high": [], "low": [], "close": []}), weight=weight)
    item["error"] = error
    item["symbol"] = str(symbol).strip().upper()
    item["weight"] = float(weight)
    item["trend"] = None
    return item


def _clip_close(prev_close: float, close: float, limit: float, atr: float) -> float:
    """Bound one day's close by limit-up/down and a 2.5×ATR excursion."""
    lo = prev_close * (1.0 - limit)
    hi = prev_close * (1.0 + limit)
    if atr > 0:
        lo = max(lo, prev_close - 2.5 * atr)
        hi = min(hi, prev_close + 2.5 * atr)
    return min(max(close, lo), hi)


def _bars_from_geometry(
    *,
    last_close: float,
    dates: list[str],
    geometry: dict[str, list[float]],
    atr: float,
    limit: float,
) -> list[dict[str, Any]]:
    """Replay weighted-mean analog geometry from the last close (no rescale)."""
    daily = list(geometry["ret"])
    prev_close = last_close
    bars: list[dict[str, Any]] = []
    for i, ts in enumerate(dates):
        gap = geometry["gap"][i] if i < len(geometry["gap"]) else 0.0
        ret = daily[i] if i < len(daily) else 0.0
        open_px = prev_close * (1.0 + gap)
        close_px = prev_close * (1.0 + ret)
        close_px = _clip_close(prev_close, close_px, limit, atr)
        # Keep the open inside the same day's limit band so the body stays drawable.
        open_px = min(max(open_px, prev_close * (1.0 - limit)), prev_close * (1.0 + limit))
        upper = geometry["upper"][i] if i < len(geometry["upper"]) else 0.002
        lower = geometry["lower"][i] if i < len(geometry["lower"]) else 0.002
        high_px = max(open_px, close_px) + abs(prev_close) * max(upper, 0.0)
        low_px = min(open_px, close_px) - abs(prev_close) * max(lower, 0.0)
        if low_px <= 0:
            low_px = min(open_px, close_px) * 0.99
        if high_px < max(open_px, close_px):
            high_px = max(open_px, close_px)
        bars.append(
            {
                "time": ts,
                "open": round(float(open_px), 4),
                "high": round(float(high_px), 4),
                "low": round(float(low_px), 4),
                "close": round(float(close_px), 4),
                "volume": 0.0,
            }
        )
        prev_close = close_px
    return bars


def _stylized_geometry(sign: int, atr: float, last_close: float, horizon: int = HORIZON) -> dict[str, list[float]]:
    """Last-resort path when a side has no analog at all: drift ≈ 0.35 ATR/day."""
    step = 0.35 * (atr / last_close) if last_close > 0 else 0.008
    step = max(0.003, min(step, 0.04))
    ret = sign * step
    return {
        "gap": [0.001 * sign] * horizon,
        "ret": [ret] * horizon,
        "upper": [0.004] * horizon,
        "lower": [0.004] * horizon,
    }


def _bar_payload(frame: pd.DataFrame, start: int | None = None) -> list[dict[str, Any]]:
    sl = frame.iloc[start:] if start is not None else frame
    rows: list[dict[str, Any]] = []
    for label, row in sl.iterrows():
        rows.append(
            {
                "time": _bar_date(label),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
                "volume": float(row["volume"]) if "volume" in sl.columns else 0.0,
            }
        )
    return rows


def _extend_ma(history_closes: np.ndarray, extra_closes: list[float], period: int) -> list[float | None]:
    """MA values on the forecast dates only (history already has its own series)."""
    combined = np.concatenate([history_closes, np.array(extra_closes, dtype=float)])
    full = _sma(combined, period)
    return full[len(history_closes) :]


def _default_assessment() -> dict[str, Any]:
    return {
        "bias": "neutral",
        "style_bucket": "low",
        "ma_structure": "mixed",
        "analog_count": 0,
        "up_count": 0,
        "down_count": 0,
        "confidence": 0.0,
        "called": False,
        "market_veto": None,
    }


def forecast_symbol(
    symbol: str,
    frame: pd.DataFrame,
    *,
    weight: float = 0.0,
    regime_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Build up/down 10-day paths and MA overlays for one holding.

    ``regime_frame`` is the concurrent CSI 300 tape used to drop neighbor
    windows from a different index regime. It is ignored when analoging the
    index itself so the barometer is not filtered by its own 20-day state.
    """
    payload: dict[str, Any] = {
        "symbol": str(symbol).strip().upper(),
        "weight": float(weight),
        "price": None,
        "up_prob": 0.5,
        "down_prob": 0.5,
        "style_score": 0.0,
        "assessment": _default_assessment(),
        "history": [],
        "up_bars": [],
        "down_bars": [],
        "ma": {
            "history": {"ma5": [], "ma10": [], "ma20": []},
            "up": {"ma5": [], "ma10": [], "ma20": []},
            "down": {"ma5": [], "ma10": [], "ma20": []},
        },
        "error": None,
    }
    try:
        ohlcv = _require_ohlcv(frame)
    except (ValueError, TypeError) as exc:
        payload["error"] = str(exc)
        return payload
    if len(ohlcv) < PATTERN_WINDOW:
        payload["error"] = "not enough daily bars for a 20-day pattern window"
        return payload

    close = ohlcv["close"].to_numpy(dtype=float)
    high = ohlcv["high"].to_numpy(dtype=float)
    low = ohlcv["low"].to_numpy(dtype=float)
    last_close = float(close[-1])
    payload["price"] = round(last_close, 4)
    last_day = pd.Timestamp(ohlcv.index[-1]).date()
    future_dates = next_weekdays(last_day, HORIZON)
    atr = _atr(high, low, close)
    limit = limit_up_pct(symbol)

    ma5_full = _sma(close, 5)
    ma10_full = _sma(close, 10)
    ma20_full = _sma(close, 20)
    ma_signs = _ma_signs(last_close, ma5_full[-1], ma10_full[-1], ma20_full[-1])
    ma_structure = _ma_structure_label(ma_signs)

    returns = _daily_returns(close)
    activity = _activity_series(ohlcv)
    # Index analog is the barometer; do not filter it by its own 20-day tape.
    regime_ok = None
    if str(symbol).strip().upper() != MARKET_INDEX:
        regime_ok = _regime_ok_mask(
            pd.DatetimeIndex(ohlcv.index),
            len(returns),
            window=PATTERN_WINDOW,
            horizon=HORIZON,
            index_frame=regime_frame,
        )
    search = search_analogs(
        returns,
        close=close,
        ma5=ma5_full,
        ma10=ma10_full,
        ma20=ma20_full,
        activity=activity,
        regime_ok=regime_ok,
        window=PATTERN_WINDOW,
        horizon=HORIZON,
        max_k=MAX_NEIGHBORS,
        min_k=MIN_NEIGHBORS,
    )
    analogs = search.neighbors
    up_matches = [m for m in analogs if m.forward_return > 0]
    down_matches = [m for m in analogs if m.forward_return < 0]

    # Softmax weights already sum to 1; zero-forward neighbors sit in neither cluster.
    up_prob = float(sum(m.weight for m in up_matches)) if analogs else 0.5
    up_prob = min(0.95, max(0.05, up_prob))
    down_prob = 1.0 - up_prob

    style_score = _style_score(analogs, search.all_similarities)
    confidence = _confidence(up_prob, style_score)

    up_path = _with_softmax_weights(_supplement_cluster(up_matches, search.pool, positive=True))
    down_path = _with_softmax_weights(_supplement_cluster(down_matches, search.pool, positive=False))
    if up_path:
        up_geo = _geometry_from_analogs(ohlcv, up_path)
    else:
        up_geo = _stylized_geometry(+1, atr, last_close)
    if down_path:
        down_geo = _geometry_from_analogs(ohlcv, down_path)
    else:
        down_geo = _stylized_geometry(-1, atr, last_close)

    up_bars = _bars_from_geometry(
        last_close=last_close,
        dates=future_dates,
        geometry=up_geo,
        atr=atr,
        limit=limit,
    )
    down_bars = _bars_from_geometry(
        last_close=last_close,
        dates=future_dates,
        geometry=down_geo,
        atr=atr,
        limit=limit,
    )

    hist_start = max(0, len(ohlcv) - HISTORY_BARS_FOR_CHART)
    history = _bar_payload(ohlcv, hist_start)
    hist_ma = {
        "ma5": ma5_full[hist_start:],
        "ma10": ma10_full[hist_start:],
        "ma20": ma20_full[hist_start:],
    }
    up_closes = [float(b["close"]) for b in up_bars]
    down_closes = [float(b["close"]) for b in down_bars]
    ma_up = {
        "ma5": _extend_ma(close, up_closes, 5),
        "ma10": _extend_ma(close, up_closes, 10),
        "ma20": _extend_ma(close, up_closes, 20),
    }
    ma_down = {
        "ma5": _extend_ma(close, down_closes, 5),
        "ma10": _extend_ma(close, down_closes, 10),
        "ma20": _extend_ma(close, down_closes, 20),
    }

    bias, called = directional_bias(up_prob, confidence, style_score)

    payload.update(
        {
            "up_prob": round(up_prob, 4),
            "down_prob": round(down_prob, 4),
            "style_score": round(style_score, 4),
            "assessment": {
                "bias": bias,
                "style_bucket": _style_bucket(style_score),
                "ma_structure": ma_structure,
                "analog_count": len(analogs),
                "up_count": len(up_matches),
                "down_count": len(down_matches),
                "confidence": round(confidence, 4),
                "called": called,
                "market_veto": None,
            },
            "history": history,
            "up_bars": up_bars,
            "down_bars": down_bars,
            "ma": {"history": hist_ma, "up": ma_up, "down": ma_down},
            "error": None,
        }
    )
    return payload


def _default_fetch(
    symbols: list[str],
    start: str,
    end: str,
    source: str,
    interval: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    return _fetch_universe_ohlcv(
        symbols, start=start, end=end, source=source, interval=interval
    )


def run_monitor_forecast(
    config: MonitorConfig,
    *,
    as_of: date | str | None = None,
    lookback_days: int = FORECAST_LOOKBACK_DAYS,
    runs_root: Path | None = None,
    fetch_ohlcv: FetchOhlcv | None = None,
    apply_live_overlay: bool | None = None,
) -> dict[str, Any]:
    """Load current (or replay-day) holdings and forecast each active name.

    ``as_of=None`` uses the live board cache. A YYYY-MM-DD string loads that
    day's snapshot and clips OHLCV through that date so replay cannot peek.
    Lookback defaults to :data:`FORECAST_LOOKBACK_DAYS` (scan stays at 400).
    """
    as_of_date = _parse_as_of(as_of)
    holdings = list(config.holdings)
    used_as_of: str | None = None
    previous_decision = getattr(config, "decision", None)

    if as_of_date is not None:
        from src.monitor.store import is_safe_history_key, load_day_snapshot

        key = _history_key(config)
        if not is_safe_history_key(key):
            raise MonitorScanError(f"invalid history key: {key!r}")
        snap = load_day_snapshot(key, as_of_date.isoformat())
        if snap is None:
            raise MonitorScanError(f"no snapshot for {as_of_date.isoformat()}")
        holdings = list(snap.holdings)
        used_as_of = as_of_date.isoformat()
        previous_decision = getattr(snap, "decision", None) or previous_decision
    else:
        used_as_of = date.today().isoformat()
        as_of_date = date.today()

    picked = active_holdings(holdings)
    capped = len(picked) > MAX_FORECAST_SYMBOLS
    picked = picked[:MAX_FORECAST_SYMBOLS]
    symbols = [row.symbol for row in picked]
    weight_by = {row.symbol: float(row.weight) for row in picked}

    if not symbols:
        return {
            "as_of": used_as_of,
            "horizon": HORIZON,
            "holding_count": 0,
            "evaluated_count": 0,
            "capped": False,
            "bias": "neutral",
            "called_count": 0,
            "market": None,
            "trend": None,
            "items": [],
        }

    root = runs_root or RUNS_DIR
    source, interval = "auto", "1D"
    if config.strategy.type == "run_dir" and (config.strategy.run_id or "").strip():
        source, interval = _run_source_interval(root / str(config.strategy.run_id).strip())

    start = (as_of_date - timedelta(days=max(30, lookback_days))).isoformat()
    fetcher = fetch_ohlcv or _default_fetch
    fetch_symbols = list(symbols)
    if MARKET_INDEX not in fetch_symbols:
        fetch_symbols.append(MARKET_INDEX)

    # Same-day live bar only when evaluating "today"; history replay stays frozen.
    should_overlay = apply_live_overlay
    if should_overlay is None:
        should_overlay = as_of is None and as_of_date == date.today()
    # Fetch through T-1 when overlay will add today's quote. Yesterday is a
    # settled parquet key; CSI 300 index_daily is 1 call/hour and must not be
    # re-hit on every forecast click with end=today (which skips disk cache).
    fetch_end = as_of_date
    if should_overlay and _is_daily_interval(interval):
        fetch_end = as_of_date - timedelta(days=1)
    end = fetch_end.isoformat()
    data_map, fetch_errors = fetcher(fetch_symbols, start, end, source, interval)

    if should_overlay and data_map and _is_daily_interval(interval):
        try:
            data_map, _, _ = _apply_same_day_overlay(data_map, as_of_date=as_of_date)
        except Exception as exc:  # noqa: BLE001 — analog still runs on T-1
            logger.warning("forecast live overlay skipped: %s", exc)

    raw_market = data_map.get(MARKET_INDEX)
    regime_frame: pd.DataFrame | None = None
    if raw_market is not None and not getattr(raw_market, "empty", True):
        clipped_market = _clip_frame(raw_market, as_of_date)
        if clipped_market is not None and not getattr(clipped_market, "empty", True):
            # Holdings reuse this clip as a neighbor filter; the index analog
            # itself is not filtered by the same tape (see forecast_symbol).
            regime_frame = clipped_market

    items: list[dict[str, Any]] = []
    for symbol in symbols:
        frame = data_map.get(symbol)
        if frame is None or getattr(frame, "empty", True):
            err = fetch_errors.get(symbol) or "no OHLCV bars returned"
            items.append(_empty_item(symbol, weight_by.get(symbol, 0.0), err))
            continue
        clipped = _clip_frame(frame, as_of_date)
        item = forecast_symbol(
            symbol,
            clipped,
            weight=weight_by.get(symbol, 0.0),
            regime_frame=regime_frame,
        )
        item["trend"] = _trend_for_item(clipped, regime_frame)
        items.append(item)

    market: dict[str, Any] | None
    if regime_frame is None:
        market = {
            "symbol": MARKET_INDEX,
            "up_prob": 0.5,
            "down_prob": 0.5,
            "style_score": 0.0,
            "confidence": 0.0,
            "bias": "neutral",
            "called": False,
            "decisive": False,
            "impact": 0.0,
            "analog_count": 0,
            "error": fetch_errors.get(MARKET_INDEX) or "no OHLCV bars returned",
        }
        logger.warning("forecast CSI 300 analog skipped: %s", market["error"])
    else:
        market_item = forecast_symbol(MARKET_INDEX, regime_frame)
        market = _market_snapshot(market_item)
        structure = _market_structure(regime_frame)
        if structure:
            market["regime"] = structure["regime"]
            market["mom120"] = structure["mom120"]
            if structure.get("horizons"):
                market["horizons"] = structure["horizons"]
        if market.get("error"):
            logger.warning("forecast CSI 300 analog failed: %s", market["error"])
        else:
            for item in items:
                if item.get("error"):
                    continue
                item["assessment"] = apply_market_veto(item.get("assessment") or {}, market)

    # Book tilt only from names that actually issued a directional call.
    called_items = [
        it
        for it in items
        if not it.get("error") and bool((it.get("assessment") or {}).get("called"))
    ]
    called_count = len(called_items)
    if not called_items:
        bias = "neutral"
    else:
        up_w = 0.0
        down_w = 0.0
        for it in called_items:
            w = abs(float(it.get("weight") or 0.0)) or 1.0
            if str((it.get("assessment") or {}).get("bias")) == "down":
                down_w += w
            else:
                up_w += w
        if up_w > down_w:
            bias = "up"
        elif down_w > up_w:
            bias = "down"
        else:
            bias = "neutral"

    market_regime = None if not market else market.get("regime")
    horizons = None if not market else market.get("horizons")
    if not isinstance(horizons, dict):
        horizons = None
    payload = {
        "as_of": used_as_of,
        "horizon": HORIZON,
        "holding_count": len(active_holdings(holdings)),
        "evaluated_count": len(items),
        "capped": capped,
        "bias": bias,
        "called_count": called_count,
        "market": market,
        "trend": {
            "market_regime": market_regime,
            "mom120": None if not market else market.get("mom120"),
            "rule": _trend_rule(str(market_regime) if market_regime else None, horizons),
            "horizons": horizons,
        },
        "items": sort_dicts_by_trend_action(items),
    }
    try:
        from src.process.attach import (
            decision_after_forecast,
            implied_book_stance,
            market_regime_from_holdings,
        )
        from src.process.models import Decision

        regime = str(market_regime) if market_regime else market_regime_from_holdings(holdings)
        prev = previous_decision
        if isinstance(prev, dict):
            prev = Decision.from_dict(prev)
        decision = decision_after_forecast(
            market_regime=regime,
            f0_stance=implied_book_stance(holdings, regime, horizons),
            holdings=holdings,
            provisional=False,
            as_of=used_as_of,
            forecast_payload=payload,
            previous=prev,
            horizons=horizons,
        )
        payload["decision"] = decision.to_dict()
    except Exception as exc:  # noqa: BLE001 — forecast paths still return without process
        logger.warning("forecast process compose skipped: %s", exc)
    return payload
