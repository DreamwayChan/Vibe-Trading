"""Rank Eastmoney boards by dual-track RS vs knife share.

The table is the observation map (RS − knife from each name's own MA stack).
The recommended pick walks that order and skips boards below their own
EW/MA20, plus catalog recipes whose already-run book is empty or leftover.
Index bull demotes the whole table to watch-only; index bear recommends
nothing new — it must not blank the RS/knife columns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.market_pick.models import BoardScore, Stance
from src.sector_pick.catalog import catalog_key_for_board, recipe_by_key

logger = logging.getLogger(__name__)

# Boards with too few classifiable names are shown but cannot be the pick.
MIN_CLASSIFIED = 8
# Same constant on every row when CSI 300 is bull — order stays RS−knife,
# displayed scores sit a full timing-gap below a chop tape.
BULL_PENALTY = 1.0


@dataclass
class CatalogBookGate:
    """Same-day veto from an already-run catalog strategy (monitor / sector-pick).

    Market-pick does not run SignalEngine. This only reads books that another
    scan already persisted for today.
    """

    book_healthy: bool | None = None
    holding_count: int | None = None
    skip_reason: str | None = None


def assign_board_scores(
    rows: list[BoardScore],
    *,
    market_regime: str | None,
    catalog_gates: dict[str, CatalogBookGate] | None = None,
) -> tuple[BoardScore | None, Stance]:
    """Fill live_score / rank and pick a recommended board under the regime.

    Rank order stays RS − knife among boards with enough names. Recommendation
    then walks that list and skips below-MA20 / empty-catalog rows.
    """
    gates = catalog_gates or {}
    scored: list[BoardScore] = []
    for row in rows:
        recipe_key = catalog_key_for_board(row.bk, row.name)
        row.catalog_key = recipe_key
        recipe = recipe_by_key(recipe_key) if recipe_key else None
        row.catalog_name = recipe.name if recipe is not None else None
        row.skip_reason = None
        if row.classified <= 0 or row.rs_share is None or row.knife_share is None:
            row.live_score = None
            row.rank = None
            continue
        score = float(row.rs_share) - float(row.knife_share)
        if market_regime == "bull":
            score -= BULL_PENALTY
        row.live_score = score
        scored.append(row)

    # Tiny boards keep a live_score for RS sorts, but they are not the map.
    for row in scored:
        row.rank = None
    sized = [row for row in scored if row.classified >= MIN_CLASSIFIED]
    ranked = sorted(
        sized,
        key=lambda row: (
            float(row.live_score or 0.0),
            float(row.rs_share or 0.0),
            -float(row.knife_share or 0.0),
            row.classified,
            row.bk,
        ),
        reverse=True,
    )
    for index, row in enumerate(ranked, start=1):
        row.rank = index
        row.skip_reason = recommend_skip(row, catalog_gates=gates)

    if market_regime == "bear" or market_regime is None:
        return None, "none"
    tradable = [row for row in ranked if not row.skip_reason]
    if not tradable:
        return None, "none" if market_regime != "chop" else "half_watch"
    winner = tradable[0]
    if market_regime == "bull":
        return winner, "half_watch"
    return winner, "full"


def recommend_skip(
    row: BoardScore,
    *,
    catalog_gates: dict[str, CatalogBookGate],
) -> str | None:
    """Why this row cannot be the day's recommended pick. None = eligible.

    Missing timing / missing catalog snapshot fail-open so a sparse first
    scan still has a tape winner. Explicit below-MA20 or an empty bound
    book fail-closed.
    """
    if row.classified < MIN_CLASSIFIED:
        return f"可分类不足 {MIN_CLASSIFIED} 只"
    # Own-board equal-weight vs MA20 — RS vs CSI 300 is not a buy signal
    # when the board itself is still below its moving average.
    if row.timing_mult is not None and float(row.timing_mult) < 1.0:
        return "等权均价低于 MA20"
    key = row.catalog_key
    if not key:
        return None
    gate = catalog_gates.get(key)
    if gate is None:
        return None
    if gate.book_healthy is False:
        return gate.skip_reason or "策略残仓"
    if gate.holding_count == 0:
        return "策略空仓"
    return None


def map_leader(rows: list[BoardScore]) -> BoardScore | None:
    """Observation-map #1: highest RS−knife among sized boards, skips included."""
    ranked = [row for row in rows if row.rank is not None]
    if not ranked:
        return None
    return min(ranked, key=lambda row: int(row.rank or 999))


def build_reason(
    winner: BoardScore | None,
    stance: Stance,
    *,
    market_regime: str | None,
    map_row: BoardScore | None = None,
    horizons: dict | None = None,
) -> str:
    """One-line Chinese copy for the hero card."""
    prefix = _regime_prefix(market_regime, horizons=horizons)
    if market_regime == "bear":
        return prefix
    if winner is None or stance == "none":
        if market_regime is None:
            return prefix + "沪深300 未分类：只展示、不推多。请刷新或检查行情缓存。"
        if map_row is not None and map_row.skip_reason:
            return (
                prefix
                + f"观察第一 {map_row.name} 已跳过（{map_row.skip_reason}）；"
                + "没有可执行的前排板块。"
            )
        return prefix + "没有可分类的前排板块。"
    rs = winner.rs_share * 100 if winner.rs_share is not None else 0.0
    knife = winner.knife_share * 100 if winner.knife_share is not None else 0.0
    bits = [f"强势 {rs:.0f}%", f"刀口 {knife:.0f}%", f"可分类 {winner.classified} 只"]
    if stance == "half_watch":
        bits.append("半仓/观望，不加仓")
    text = prefix + f"{winner.name}：" + "；".join(bits)
    if winner.catalog_name:
        text += f"。已有最优回测「{winner.catalog_name}」，可去优选板块跟踪。"
    if map_row is not None and map_row.bk != winner.bk and map_row.skip_reason:
        text += f"。观察第一 {map_row.name} 已跳过（{map_row.skip_reason}）"
    return text


def load_catalog_gates(as_of: str) -> dict[str, CatalogBookGate]:
    """Read today's catalog books from sector-pick and monitor snapshots.

    Fail-soft: a missing store must not abort the tape screen.
    """
    gates: dict[str, CatalogBookGate] = {}
    _merge_sector_pick_gates(gates)
    _merge_monitor_gates(gates, as_of)
    return gates


def _merge_sector_pick_gates(gates: dict[str, CatalogBookGate]) -> None:
    try:
        from src.sector_pick.store import load_state
    except Exception as exc:  # noqa: BLE001 — tape screen still ranks
        logger.debug("market-pick sector-pick gates unavailable: %s", exc)
        return
    try:
        result = load_state().result
    except Exception as exc:  # noqa: BLE001
        logger.debug("market-pick sector-pick state unreadable: %s", exc)
        return
    if result is None:
        return
    for row in result.sectors:
        if row.book_healthy is None and row.holding_count is None:
            continue
        gates[row.key] = CatalogBookGate(
            book_healthy=row.book_healthy,
            holding_count=row.holding_count,
            skip_reason=row.skip_reason,
        )


def _merge_monitor_gates(gates: dict[str, CatalogBookGate], as_of: str) -> None:
    """Same-day monitor books override sector-pick: that is the engine truth."""
    try:
        from src.monitor.store import load_day_snapshot
        from src.sector_pick.catalog import CATALOG
        from src.sector_pick.score import evaluate_book
    except Exception as exc:  # noqa: BLE001
        logger.debug("market-pick monitor gates unavailable: %s", exc)
        return
    for recipe in CATALOG:
        try:
            snap = load_day_snapshot(recipe.run_id, as_of)
        except Exception as exc:  # noqa: BLE001 — one dead run must not abort
            logger.debug("market-pick monitor snapshot %s: %s", recipe.run_id, exc)
            continue
        if snap is None:
            continue
        count, _max_weight, skip = evaluate_book(snap.holdings)
        empty = count == 0
        wait_flat, wait_note = _decision_is_wait_flat(snap.decision)
        unhealthy = empty or skip is not None or wait_flat
        gates[recipe.key] = CatalogBookGate(
            book_healthy=False if unhealthy else True,
            holding_count=0 if wait_flat or empty else count,
            skip_reason=wait_note or skip or ("策略空仓" if empty else None),
        )


def _decision_is_wait_flat(decision: object | None) -> tuple[bool, str | None]:
    """True when today's monitor Decision is wait / 0% NAV (not executable)."""
    if decision is None:
        return False, None
    try:
        size = float(getattr(decision, "size_nav", 0.0) or 0.0)
    except (TypeError, ValueError):
        size = 0.0
    action = str(getattr(decision, "action", "") or "").strip().lower()
    if action == "wait" or size <= 0:
        return True, "等待，建议规模 0%"
    return False, None


def _regime_prefix(market_regime: str | None, horizons: dict | None = None) -> str:
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
        return "沪深300中期震荡：相对强势前排、刀口后排。"
    return ""
