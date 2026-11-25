"""A-share whole-market breadth & money-making effect thermometer (赚钱效应温度计).

CSI 300 alone can stay green on bank/megacap support while 3,000 small/mid-cap
names drop. This module computes causal breadth indicators directly from an
already-loaded ``data_map`` or from settled ``bars_daily`` rows in the local
data lake — requiring zero paid vendor APIs and zero network calls.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

import numpy as np
import pandas as pd

from src.data.tradability import price_limit_pct

logger = logging.getLogger(__name__)

BreadthState = Literal["warm", "neutral", "cold"]
StanceCap = Literal["full", "half_watch", "none"]

# Minimum cross-sectional names before breadth is allowed to tighten a stance.
# Small unit-test fixtures (1-10 tickers) stay descriptive-only.
MIN_SAMPLE_FOR_GATE = 30
_INDEX_CODES = frozenset({"000300.SH", "000905.SH", "000852.SH", "399006.SZ", "000001.SH"})


@dataclass(frozen=True)
class BreadthSnapshot:
    """Cross-sectional market breadth as of one session."""

    as_of: str
    sample_size: int
    sufficient_sample: bool
    adv_ratio: float | None = None
    above_ma20_ratio: float | None = None
    limit_up_count: int = 0
    limit_down_count: int = 0
    prev_limit_up_count: int = 0
    prev_limit_up_premium: float | None = None
    volume_ma20_ratio: float | None = None
    median_return: float | None = None
    state: BreadthState = "neutral"
    stance_cap: StanceCap = "full"
    score: float = 0.5
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> BreadthSnapshot | None:
        if not isinstance(raw, Mapping):
            return None
        as_of = str(raw.get("as_of") or "").strip()
        if not as_of:
            return None
        state = str(raw.get("state") or "neutral").strip().lower()
        if state not in ("warm", "neutral", "cold"):
            state = "neutral"
        stance_cap = str(raw.get("stance_cap") or "full").strip().lower()
        if stance_cap not in ("full", "half_watch", "none"):
            stance_cap = "full"
        return cls(
            as_of=as_of,
            sample_size=int(raw.get("sample_size") or 0),
            sufficient_sample=bool(raw.get("sufficient_sample", False)),
            adv_ratio=_opt_float(raw.get("adv_ratio")),
            above_ma20_ratio=_opt_float(raw.get("above_ma20_ratio")),
            limit_up_count=int(raw.get("limit_up_count") or 0),
            limit_down_count=int(raw.get("limit_down_count") or 0),
            prev_limit_up_count=int(raw.get("prev_limit_up_count") or 0),
            prev_limit_up_premium=_opt_float(raw.get("prev_limit_up_premium")),
            volume_ma20_ratio=_opt_float(raw.get("volume_ma20_ratio")),
            median_return=_opt_float(raw.get("median_return")),
            state=state,  # type: ignore[arg-type]
            stance_cap=stance_cap,  # type: ignore[arg-type]
            score=float(_opt_float(raw.get("score")) or 0.5),
            summary=str(raw.get("summary") or ""),
        )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if np.isfinite(num) else None


def compute_breadth_from_map(
    data_map: Mapping[str, pd.DataFrame] | None,
    *,
    as_of: str,
    min_sample: int = MIN_SAMPLE_FOR_GATE,
) -> BreadthSnapshot:
    """Compute cross-sectional breadth on or before ``as_of`` from ``data_map``."""
    if not data_map:
        return BreadthSnapshot(
            as_of=as_of,
            sample_size=0,
            sufficient_sample=False,
            summary="尚无截面行情样本，赚钱效应按中性处理。",
        )

    cutoff = pd.Timestamp(as_of).normalize()
    returns: list[float] = []
    above_ma20 = 0
    ma20_total = 0
    limit_up = 0
    limit_down = 0
    prev_lu_Follow_rets: list[float] = []
    vol_today_sum = 0.0
    vol_ma20_sum = 0.0

    for raw_symbol, frame in data_map.items():
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in _INDEX_CODES or frame is None or getattr(frame, "empty", True):
            continue
        if "close" not in frame.columns:
            continue
        idx = pd.to_datetime(frame.index, errors="coerce").normalize()
        sub = frame.loc[idx <= cutoff]
        if len(sub) < 2:
            continue

        closes = pd.to_numeric(sub["close"], errors="coerce").to_numpy(dtype=float)
        c_now = closes[-1]
        c_prev = closes[-2]
        if not (np.isfinite(c_now) and np.isfinite(c_prev) and c_now > 0 and c_prev > 0):
            continue

        if "volume" in sub.columns:
            vols = pd.to_numeric(sub["volume"], errors="coerce").to_numpy(dtype=float)
            v_now = vols[-1]
            if not np.isfinite(v_now) or v_now <= 0:
                continue  # suspended
            if len(vols) >= 20:
                v_win = vols[-20:]
                valid_v = v_win[np.isfinite(v_win) & (v_win > 0)]
                if len(valid_v) >= 10:
                    vol_today_sum += float(v_now)
                    vol_ma20_sum += float(np.mean(valid_v))

        ret = c_now / c_prev - 1.0
        returns.append(ret)

        if len(closes) >= 20:
            win20 = closes[-20:]
            if np.isfinite(win20).all():
                ma20_total += 1
                if c_now >= float(np.mean(win20)):
                    above_ma20 += 1

        band = price_limit_pct(symbol, is_st=False)
        if ret >= band - 0.004:
            limit_up += 1
        elif ret <= -band + 0.004:
            limit_down += 1

        # Yesterday limit-up -> today's follow-through return (接力溢价率).
        if len(closes) >= 3:
            c_prev2 = closes[-3]
            if np.isfinite(c_prev2) and c_prev2 > 0:
                prev_ret = c_prev / c_prev2 - 1.0
                if prev_ret >= band - 0.004:
                    prev_lu_Follow_rets.append(ret)

    n = len(returns)
    sufficient = n >= max(1, int(min_sample))
    if n == 0:
        return BreadthSnapshot(
            as_of=as_of,
            sample_size=0,
            sufficient_sample=False,
            summary="无有效交易截面样本，赚钱效应按中性处理。",
        )

    arr = np.asarray(returns, dtype=float)
    adv_ratio = round(float(np.mean(arr > 0.0)), 4)
    median_ret = round(float(np.median(arr)), 4)
    above_ma20_ratio = round(above_ma20 / ma20_total, 4) if ma20_total > 0 else None
    prev_lu_premium = (
        round(float(np.mean(prev_lu_Follow_rets)), 4) if prev_lu_Follow_rets else None
    )
    vol_ratio = (
        round(vol_today_sum / vol_ma20_sum, 4)
        if vol_ma20_sum > 0 and vol_today_sum > 0
        else None
    )

    state, stance_cap, score = _classify_breadth(
        sufficient=sufficient,
        sample_size=n,
        adv_ratio=adv_ratio,
        above_ma20_ratio=above_ma20_ratio,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        prev_lu_premium=prev_lu_premium,
    )
    summary = _build_summary(
        state=state,
        sufficient=sufficient,
        sample_size=n,
        adv_ratio=adv_ratio,
        above_ma20_ratio=above_ma20_ratio,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        prev_lu_premium=prev_lu_premium,
        vol_ratio=vol_ratio,
    )
    return BreadthSnapshot(
        as_of=as_of,
        sample_size=n,
        sufficient_sample=sufficient,
        adv_ratio=adv_ratio,
        above_ma20_ratio=above_ma20_ratio,
        limit_up_count=limit_up,
        limit_down_count=limit_down,
        prev_limit_up_count=len(prev_lu_Follow_rets),
        prev_limit_up_premium=prev_lu_premium,
        volume_ma20_ratio=vol_ratio,
        median_return=median_ret,
        state=state,
        stance_cap=stance_cap,
        score=round(score, 4),
        summary=summary,
    )


def load_breadth_from_lake(
    as_of: str,
    *,
    min_sample: int = MIN_SAMPLE_FOR_GATE,
) -> BreadthSnapshot | None:
    """Read the last 25 sessions of ``bars_daily`` from the local lake to score breadth.

    Never touches the network; returns ``None`` when the lake has insufficient coverage.
    """
    try:
        from src.data.lake import dataset_exists, read_dataset

        if not dataset_exists("bars_daily"):
            return None
        end_ts = pd.Timestamp(as_of).normalize()
        start_ts = end_ts - pd.Timedelta(days=45)
        years = sorted({start_ts.year, end_ts.year})
        frame = read_dataset(
            "bars_daily",
            columns=["symbol", "date", "close", "volume"],
            filters={"year": years},
            where=('"date" >= ? AND "date" <= ?', [start_ts, end_ts]),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("lake breadth read skipped: %s", exc)
        return None
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return None

    grouped: dict[str, pd.DataFrame] = {}
    frame = frame.sort_values(["symbol", "date"])
    for sym, part in frame.groupby("symbol", sort=False):
        sub = part.set_index(pd.to_datetime(part["date"]))[["close", "volume"]]
        grouped[str(sym)] = sub
    snap = compute_breadth_from_map(grouped, as_of=as_of, min_sample=min_sample)
    return snap if snap.sufficient_sample else None


def _classify_breadth(
    *,
    sufficient: bool,
    sample_size: int,
    adv_ratio: float,
    above_ma20_ratio: float | None,
    limit_up_count: int,
    limit_down_count: int,
    prev_lu_premium: float | None,
) -> tuple[BreadthState, StanceCap, float]:
    ma20 = above_ma20_ratio if above_ma20_ratio is not None else adv_ratio
    lu_total = limit_up_count + limit_down_count
    lu_share = (limit_up_count / lu_total) if lu_total > 0 else 0.5
    prem_score = 0.5
    if prev_lu_premium is not None:
        prem_score = float(np.clip(0.5 + prev_lu_premium * 10.0, 0.0, 1.0))

    raw_score = 0.40 * adv_ratio + 0.30 * ma20 + 0.15 * lu_share + 0.15 * prem_score
    score = float(np.clip(raw_score, 0.0, 1.0))

    if not sufficient:
        return "neutral", "full", 0.5

    # Panic / ice-point conditions in A-shares: broad selloff or limit-down wave.
    limit_down_wave = (
        limit_down_count >= max(15, int(sample_size * 0.02))
        and limit_down_count > limit_up_count
    )
    nuclear_relay = (
        prev_lu_premium is not None
        and prev_lu_premium <= -0.025
        and adv_ratio < 0.35
    )
    if adv_ratio < 0.24 or limit_down_wave or nuclear_relay or (adv_ratio < 0.30 and ma20 < 0.25):
        return "cold", "none", min(score, 0.25)

    if (
        adv_ratio >= 0.52
        and ma20 >= 0.45
        and not (limit_down_count > limit_up_count and limit_down_count >= 10)
        and (prev_lu_premium is None or prev_lu_premium >= -0.005)
    ):
        return "warm", "full", max(score, 0.68)

    if adv_ratio < 0.38 or (prev_lu_premium is not None and prev_lu_premium < -0.012):
        return "neutral", "half_watch", score
    return "neutral", "full", score


def _build_summary(
    *,
    state: BreadthState,
    sufficient: bool,
    sample_size: int,
    adv_ratio: float,
    above_ma20_ratio: float | None,
    limit_up_count: int,
    limit_down_count: int,
    prev_lu_premium: float | None,
    vol_ratio: float | None,
) -> str:
    if not sufficient:
        return f"截面样本仅 {sample_size} 只（不足 {MIN_SAMPLE_FOR_GATE} 只），赚钱效应仅供展示、不额外限仓。"
    head = {
        "warm": "赚钱效应偏暖（顺风可出手）",
        "neutral": "赚钱效应分化（控制仓位精选）",
        "cold": "亏钱效应预警（退潮冰点，暂停新开仓）",
    }[state]
    bits = [f"上涨占比 {adv_ratio * 100:.0f}%"]
    if above_ma20_ratio is not None:
        bits.append(f"站上MA20占 {above_ma20_ratio * 100:.0f}%")
    bits.append(f"涨跌停 {limit_up_count}:{limit_down_count}")
    if prev_lu_premium is not None:
        bits.append(f"昨涨停溢价 {prev_lu_premium * 100:+.2f}%")
    if vol_ratio is not None:
        bits.append(f"量能/20日均 {vol_ratio:.2f}x")
    return f"{head}：{' · '.join(bits)}（样本 {sample_size} 只）。"
