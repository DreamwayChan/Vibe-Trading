"""ETF-OI model: how much of the CSI 300 float the index funds hold, and how
much money moved through them each day.

Two layers:

1. **Stock (stock of capital)** — ``AUM / CSI300 free-float market cap``. This
   is the ETF-OI ratio popularised by 光大证券 (2024Q4) and used as the
   denominator for the "national team" debate: index funds are the marginal
   buyer whose share of the market keeps rising.
2. **Flow (flow of capital)** — the daily share change of those funds, priced at
   the previous NAV, gives net subscriptions. 国金证券 pairs this tape with
   中央汇金's quarterly disclosed holdings; the holdings file is not machine
   readable, so this module ships the flow leg only, plus the cumulative
   subscription it implies. Treat the result as a *proxy*, never as a
   confirmed position.

Everything here is deterministic and computed from already-aligned daily data;
no forward-looking interpolation is used.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from src.capital import sources
from src.capital.models import (
    CapitalPoint,
    EtfFundDef,
    EtfOiReport,
    FlowStats,
    FundDailyRow,
    McapReference,
)
from src.capital.universe import INDEX_LABEL, universe, universe_meta

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 120
# A flow sum needs at least this many observations to be reported.
_MIN_FLOW_OBS_5D = 3
_MIN_FLOW_OBS_20D = 10
_MIN_Z_OBS = 20


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    try:
        out = numerator / denominator
    except ZeroDivisionError:  # pragma: no cover - guarded above
        return None
    return out if math.isfinite(out) else None


def _asof(series: dict[str, float], day: str) -> float | None:
    """Value of ``series`` as of ``day`` (exact hit or carried forward)."""
    value = series.get(day)
    if value is not None:
        return value
    # NAV is published after the close; a fund suspended for a day keeps its
    # previous NAV, so a short carry-forward is the right behaviour.
    candidates = [d for d in series if d <= day]
    if not candidates:
        return None
    return series[max(candidates)]


def _ffill(rows: list[tuple[str, dict[str, float]]], codes: Iterable[str]) -> dict[str, dict[str, float]]:
    """Carry the last observed share count forward, per fund."""
    last: dict[str, float] = {}
    out: dict[str, dict[str, float]] = {}
    for day, snapshot in rows:
        for code in codes:
            value = snapshot.get(code)
            if value is not None:
                last[code] = value
        out[day] = dict(last)
    return out


def build_series(
    window: int = DEFAULT_WINDOW,
    mcap_mode: str = "circ_mv",
    codes: str | None = None,
    allow_stale: bool = True,
) -> EtfOiReport:
    """Build the ETF-OI report.

    Args:
        window: Number of trading days to load (bounded by ``sources.clamp_window``).
        mcap_mode: ``circ_mv`` (Σ 流通市值, default) or ``weight_implied``
            (median of 流通市值 / 官方权重).
        codes: Optional comma separated fund-code override.
        allow_stale: When every network source fails, fall back to the last
            persisted report instead of raising.

    Raises:
        sources.CapitalSourceError: when the index or the market-cap anchor is
            unavailable and no persisted report exists.
    """
    window = sources.clamp_window(window)
    mode = "weight_implied" if str(mcap_mode).strip().lower() == "weight_implied" else "circ_mv"
    funds = universe(codes)
    warnings: list[str] = []

    index_series = sources.index_close_history()
    days = sources.trading_days(index_series, window)
    if len(days) < 2:
        raise sources.CapitalSourceError("index history too short to build a series")

    # --- fund NAVs -------------------------------------------------------
    navs = sources.prefetch_nav([f.code for f in funds])
    usable = [f for f in funds if navs.get(f.code)]
    for fund in funds:
        if not navs.get(fund.code):
            warnings.append(f"净值缺失：{fund.code} {fund.name}（已从样本剔除）")
    if not usable:
        raise sources.CapitalSourceError("no fund NAV available")

    # --- shares ----------------------------------------------------------
    sh_funds = [f for f in usable if f.exchange == "SH"]
    sz_funds = [f for f in usable if f.exchange == "SZ"]
    sse_map: dict[str, dict[str, float]] = {}
    if sh_funds:
        sse_map = sources.prefetch_sse_shares(days)
    szse_map: dict[str, dict[str, float]] = {}
    if sz_funds:
        try:
            szse_map = sources.szse_etf_shares(days[0], days[-1])
        except sources.CapitalSourceError as exc:
            warnings.append(f"深交所份额不可用：{exc}")

    snapshot_rows: list[tuple[str, dict[str, float]]] = []
    for day in days:
        merged: dict[str, float] = {}
        merged.update(sse_map.get(day) or {})
        merged.update(szse_map.get(day) or {})
        snapshot_rows.append((day, merged))

    forward = _ffill(snapshot_rows, [f.code for f in usable])

    # --- market-cap anchor ----------------------------------------------
    index_last_day = max(index_series)
    index_last = index_series[index_last_day]
    mcap_raw = sources.csi300_market_cap(index_last)
    total_key = "total_weight_implied" if mode == "weight_implied" else "total_circ_mv"
    mcap_total = float(mcap_raw.get(total_key) or 0.0)
    if mcap_total <= 0:
        raise sources.CapitalSourceError("CSI 300 market cap is zero — cannot compute ETF OI")
    per_point = mcap_total / index_last
    mcap_ref = McapReference(
        date=str(mcap_raw.get("as_of") or index_last_day),
        weights_date=str(mcap_raw.get("weights_date") or ""),
        index_close=index_last,
        per_point=per_point,
        total=mcap_total,
        mode=mode,  # type: ignore[arg-type]
        total_circ_mv=float(mcap_raw.get("total_circ_mv") or 0.0) or None,
        total_weight_implied=float(mcap_raw.get("total_weight_implied") or 0.0) or None,
        constituents=int(mcap_raw.get("constituents") or 0),
        covered=int(mcap_raw.get("covered") or 0),
    )
    if mcap_ref.covered and mcap_ref.constituents and mcap_ref.covered < mcap_ref.constituents:
        warnings.append(
            f"成分股市值覆盖 {mcap_ref.covered}/{mcap_ref.constituents}，分母按已覆盖部分估算"
        )

    # --- per-day aggregation --------------------------------------------
    points: list[CapitalPoint] = []
    prev: dict[str, dict[str, float]] | None = None
    last_rows: list[FundDailyRow] = []
    last_prev: dict[str, dict[str, float]] = {}
    stale_days = 0
    for day in days:
        reported = set(sse_map.get(day) or {}) | set(szse_map.get(day) or {})
        shares_today = forward.get(day) or {}
        rows: list[FundDailyRow] = []
        stale = 0
        nav_missing = 0
        for fund in usable:
            shares = shares_today.get(fund.code)
            if shares is None:
                # Not listed yet on this day (or the bourse never published it).
                continue
            nav = _asof(navs[fund.code], day)
            if nav is None:
                nav_missing += 1
                continue
            is_stale = fund.code not in reported
            if is_stale:
                stale += 1
            rows.append(
                FundDailyRow(
                    code=fund.code,
                    name=fund.name,
                    exchange=fund.exchange,
                    shares=shares,
                    nav=nav,
                    aum=shares * nav,
                    stale=is_stale,
                )
            )

        aum = sum(r.aum for r in rows if r.aum is not None)
        shares_total = sum(r.shares for r in rows if r.shares is not None)
        index_close = index_series.get(day)
        index_mcap = per_point * index_close if index_close else None
        missing = stale + nav_missing

        net_flow: float | None = None
        nav_pnl: float | None = None
        flow_bp: float | None = None
        # A flow needs a clean pair of days: no carried-forward shares and a
        # previous observation for every fund in today's book.
        if prev is not None and missing == 0 and rows and all(r.code in prev for r in rows):
            net_flow = 0.0
            nav_pnl = 0.0
            for row in rows:
                before = prev[row.code]
                net_flow += (row.shares - before["shares"]) * before["nav"]
                nav_pnl += before["shares"] * (row.nav - before["nav"])
            if index_mcap:
                flow_bp = net_flow / index_mcap * 10_000.0

        provisional = bool(missing)
        if provisional:
            stale_days += 1
        points.append(
            CapitalPoint(
                date=day,
                aum=aum if rows else None,
                shares_total=shares_total if rows else None,
                index_close=index_close,
                index_mcap=index_mcap,
                oi_pct=(aum / index_mcap * 100.0) if (rows and index_mcap) else None,
                net_flow=net_flow,
                nav_pnl=nav_pnl,
                flow_bp=flow_bp,
                fund_count=len(rows),
                missing_count=missing,
                provisional=provisional,
            )
        )
        if rows:
            # Keep the newest day that actually has a book (trailing unpublished
            # days are trimmed below).
            last_rows = rows
            last_prev = prev or {}
        prev = {r.code: {"shares": r.shares, "nav": r.nav} for r in rows}

    if stale_days:
        warnings.append(
            f"{stale_days} 个交易日存在份额未披露的基金；这些日期的 AUM 沿用前值，且不计算当日净申购"
        )
    # A day with no fund data at all (the bourse has not opened its file yet)
    # carries no information — drop it so `as_of` and the KPI strip point at the
    # last real observation instead of a column of dashes.
    while points and points[-1].aum is None:
        points.pop()
    if not points:
        raise sources.CapitalSourceError("no exchange share data in the requested window")
    no_shares = [f for f in usable if not any((forward.get(d) or {}).get(f.code) for d in days)]
    for fund in no_shares:
        warnings.append(f"份额缺失：{fund.code} {fund.name}（窗口内无交易所份额，已忽略）")

    # --- latest fund detail ---------------------------------------------
    latest_day = points[-1].date if points else ""
    latest_rows = last_rows
    for row in latest_rows:
        before = last_prev.get(row.code)
        if not before:
            continue
        row.shares_prev = before["shares"]
        row.nav_prev = before["nav"]
        row.aum_prev = before["shares"] * before["nav"]
        row.net_flow = (row.shares - before["shares"]) * before["nav"]
        row.nav_pnl = before["shares"] * (row.nav - before["nav"])
    group_aum = sum(r.aum or 0.0 for r in latest_rows)
    for row in latest_rows:
        row.group_weight = _safe_div(row.aum, group_aum)
    latest_rows.sort(key=lambda r: r.aum or 0.0, reverse=True)

    stats = compute_stats(points)
    report = EtfOiReport(
        as_of=latest_day,
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        window=window,
        points=points,
        funds=latest_rows,
        stats=stats,
        mcap=mcap_ref,
        methodology=build_methodology(mode, funds, mcap_ref, warnings),
        warnings=warnings,
        provisional=bool(points and points[-1].provisional),
        universe_size=len(usable),
    )
    return report


def compute_stats(points: list[CapitalPoint]) -> FlowStats:
    """Derive the KPI strip, z-score, streak and regime from the series."""
    stats = FlowStats(observed_days=len(points))
    if not points:
        return stats

    latest = points[-1]
    stats.oi_pct = latest.oi_pct
    stats.aum = latest.aum
    stats.net_flow = latest.net_flow
    if len(points) >= 2:
        prev = points[-2]
        if latest.oi_pct is not None and prev.oi_pct is not None:
            stats.oi_change_bp = (latest.oi_pct - prev.oi_pct) * 100.0
        if latest.aum is not None and prev.aum is not None:
            stats.aum_change = latest.aum - prev.aum

    flows = [p.net_flow for p in points if p.net_flow is not None]
    stats.flow_days = len(flows)
    stats.flow_window = sum(flows) if flows else None
    stats.flow_5d = _window_sum(flows, 5, _MIN_FLOW_OBS_5D)
    stats.flow_20d = _window_sum(flows, 20, _MIN_FLOW_OBS_20D)

    if latest.index_mcap:
        if stats.flow_5d is not None:
            stats.flow_5d_bp = stats.flow_5d / latest.index_mcap * 10_000.0
        if stats.flow_20d is not None:
            stats.flow_20d_bp = stats.flow_20d / latest.index_mcap * 10_000.0

    bp_series = [p.flow_bp for p in points if p.flow_bp is not None]
    if len(bp_series) >= _MIN_Z_OBS:
        stats.flow_z = _zscore(bp_series[-1], bp_series)
        if stats.flow_z is not None:
            stats.flow_z = max(-4.0, min(4.0, stats.flow_z))

    stats.inflow_streak = _streak([p.net_flow for p in points])

    oi_series = [p.oi_pct for p in points if p.oi_pct is not None]
    if oi_series:
        stats.oi_percentile = _percentile_rank(oi_series[-1], oi_series)

    stats.score = _score(stats)
    stats.regime = _regime(stats.score)
    return stats


def _window_sum(flows: list[float], window: int, min_obs: int) -> float | None:
    tail = flows[-window:]
    if len(tail) < min_obs:
        return None
    return float(sum(tail))


def _zscore(value: float, series: list[float]) -> float | None:
    if len(series) < _MIN_Z_OBS:
        return None
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / len(series)
    if var <= 0:
        return None
    return (value - mean) / math.sqrt(var)


def _streak(flows: list[float | None]) -> int:
    """Signed run length of same-sign flows at the end of the series.

    Trailing ``None`` values (a day whose shares are still being published) are
    skipped so the streak still describes the last settled observation.
    """
    sign = 0
    count = 0
    for value in reversed(flows):
        if value is None:
            continue
        if value == 0:
            break
        current = 1 if value > 0 else -1
        if sign == 0:
            sign = current
            count = 1
        elif current == sign:
            count += 1
        else:
            break
    return sign * count


def _percentile_rank(value: float, series: list[float]) -> float | None:
    if not series:
        return None
    below = sum(1 for x in series if x < value)
    equal = sum(1 for x in series if x == value)
    return (below + 0.5 * equal) / len(series) * 100.0


def _score(stats: FlowStats) -> float | None:
    """0-100 composite read: z-score, 20-day intensity and the streak."""
    parts: list[tuple[float, float]] = []
    if stats.flow_z is not None:
        parts.append((50.0 + 50.0 * math.tanh(stats.flow_z / 2.0), 0.4))
    if stats.flow_20d_bp is not None:
        parts.append((50.0 + 50.0 * math.tanh(stats.flow_20d_bp / 40.0), 0.4))
    if stats.inflow_streak:
        parts.append((50.0 + 50.0 * math.tanh(stats.inflow_streak / 4.0), 0.2))
    if not parts:
        return None
    total_weight = sum(w for _, w in parts)
    return round(sum(value * weight for value, weight in parts) / total_weight, 1)


def _regime(score: float | None) -> str:
    if score is None:
        return "neutral"
    if score >= 72:
        return "surge"
    if score >= 58:
        return "inflow"
    if score >= 42:
        return "neutral"
    if score >= 28:
        return "outflow"
    return "drain"


def build_methodology(
    mode: str,
    funds: list[EtfFundDef],
    mcap: McapReference,
    warnings: list[str],
) -> dict[str, Any]:
    """Formula + data-source + caveat block for the UI's methodology panel."""
    denominator_label = (
        "中证权重反推的调整市值（自由流通口径）" if mode == "weight_implied" else "成分股流通市值合计"
    )
    return {
        "index": {"code": "000300", "label": INDEX_LABEL},
        "formulas": {
            "aum": "Σ(ETF份额 × 单位净值)",
            "oi": "ETF合计规模 ÷ 沪深300流通市值 × 100%",
            "net_flow": "(份额_t − 份额_t−1) × 净值_t−1",
            "nav_pnl": "份额_t−1 × (净值_t − 净值_t−1)",
            "mcap": f"每点市值 × 指数收盘（每点市值由 {mcap.date} 成分股快照标定：{denominator_label}）",
        },
        "denominator": {
            "mode": mode,
            "label": denominator_label,
            "total": mcap.total,
            "per_point": mcap.per_point,
            "weights_date": mcap.weights_date,
            "constituents": mcap.constituents,
            "covered": mcap.covered,
            "circ_mv_total": mcap.total_circ_mv,
            "weight_implied_total": mcap.total_weight_implied,
        },
        "universe": universe_meta()["funds"],
        "sources": sources.describe_sources(),
        "caveats": [
            "分母按最新的成分股快照标定为“每点市值”，历史日按指数涨跌回推；指数成分调整与除权会带来小幅漂移（约每年 1%–2%）。",
            "“流通市值”取自公开行情接口，含大股东可流通但未减持的股份；中证官方自由流通口径更低，因此 OI 水平可能被小幅低估，可切换“权重反推”口径对照。",
            "样本仅含场内沪深300ETF：联接基金主要持有对应ETF，计入会重复统计，故排除；增强型及300价值/成长/红利等衍生指数产品不在样本内。",
            "交易所份额为盘后披露：最新一日份额缺失时，AUM 沿用前值且不计算当日净申购（页面标记“待披露”）。",
            "份额拆分/折算会造成单日净申购异常，需结合公告复核。",
            "净申购是“国家队”资金的代理指标而非直接持仓：汇金等机构也可能二级市场买入，且与公募、保险、个人的申赎混同。",
        ],
        "warnings": list(warnings),
    }
