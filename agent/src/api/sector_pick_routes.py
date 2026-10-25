"""Sector-pick HTTP routes (catalog + live ranking + winner holdings).

Mounted by ``agent/api_server.py`` via ``register_sector_pick_routes(app)``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.monitor.models import HoldingRow
from src.sector_pick.catalog import CATALOG, recipe_by_key, validate_enabled_keys
from src.sector_pick.models import SectorPickResult, SectorPickState
from src.sector_pick.scan import SectorPickError, load_sector_holdings, run_sector_pick_scan
from src.sector_pick.score import stamp_book
from src.sector_pick.theme_lock import ThemeLock
from src.sector_pick.store import (
    commit_scan_result,
    list_history_series,
    list_snapshot_summaries,
    load_day_snapshot,
    load_state,
    save_state,
)
from src.process.attach import clip_holdings

logger = logging.getLogger(__name__)


class CatalogItemResponse(BaseModel):
    key: str
    name: str
    panel: str
    confirm_ids: List[str]
    use_max_veto: bool
    run_id: str
    prop_file: str
    variant: str
    # Monitor strategy-source title (req.json rename); board ``name`` stays as-is.
    run_title: Optional[str] = None
    run_missing: bool = False


class SectorScoreResponse(BaseModel):
    key: str
    name: str
    panel: str
    run_id: str
    variant: str
    enabled: bool = True
    timing_mult: Optional[float] = None
    ew_ma20_ratio: Optional[float] = None
    ret_20d: Optional[float] = None
    confirm_pass_rate: Optional[float] = None
    live_score: Optional[float] = None
    rank: Optional[int] = None
    universe_size: int = 0
    fetch_error_count: int = 0
    excess_return: Optional[float] = None
    sharpe: Optional[float] = None
    error: Optional[str] = None
    holding_count: Optional[int] = None
    max_weight: Optional[float] = None
    book_healthy: Optional[bool] = None
    skip_reason: Optional[str] = None
    rs_n: int = 0
    dip_n: int = 0
    knife_n: int = 0
    wait_n: int = 0
    sell_n: int = 0
    rs_share: Optional[float] = None
    knife_share: Optional[float] = None


class TrendPlanResponse(BaseModel):
    """Long-only dual-track plan. Optional on old snapshots."""

    market_regime: str = ""
    stock_regime: str = ""
    swing: str = ""
    action: str = ""
    fade_rallies: bool = True
    reason: str = ""


class HoldingResponse(BaseModel):
    symbol: str
    weight: float
    action: str = "hold"
    price: Optional[float] = None
    data_as_of: Optional[str] = None
    provisional: bool = False
    prev_weight: Optional[float] = None
    trend: Optional[TrendPlanResponse] = None


class DayOpsResponse(BaseModel):
    entered: List[str] = Field(default_factory=list)
    exited: List[str] = Field(default_factory=list)
    held: List[str] = Field(default_factory=list)


class SectorPickResultResponse(BaseModel):
    as_of: str
    scanned_at: str
    recommended_key: Optional[str] = None
    holdings_key: Optional[str] = None
    stance: str = "none"
    reason: str = ""
    sectors: List[SectorScoreResponse] = Field(default_factory=list)
    holdings: List[HoldingResponse] = Field(default_factory=list)
    ops: DayOpsResponse = Field(default_factory=DayOpsResponse)
    holdings_run_id: Optional[str] = None
    provisional: bool = False
    market_regime: Optional[str] = None
    mom120: Optional[float] = None
    decision: Optional[Dict[str, Any]] = None
    locked_key: Optional[str] = None
    map_key: Optional[str] = None
    challenger_key: Optional[str] = None
    challenger_days: int = 0
    challenger_as_of: Optional[str] = None
    lock_event: Optional[str] = None
    lock_reason: Optional[str] = None


class SectorPickResponse(BaseModel):
    enabled_keys: List[str]
    catalog: List[CatalogItemResponse]
    result: Optional[SectorPickResultResponse] = None


class HistorySummaryResponse(BaseModel):
    as_of: str
    scanned_at: str
    recommended_key: Optional[str] = None
    holdings_key: Optional[str] = None
    stance: str = "none"
    provisional: bool = False
    holding_count: int = 0
    entered_count: int = 0
    exited_count: int = 0
    held_count: int = 0


class HistorySeriesPointResponse(BaseModel):
    key: str
    name: str
    rank: Optional[int] = None
    ret_20d: Optional[float] = None
    confirm_pass_rate: Optional[float] = None
    timing_mult: Optional[float] = None
    live_score: Optional[float] = None


class HistorySeriesDayResponse(BaseModel):
    as_of: str
    recommended_key: Optional[str] = None
    stance: str = "none"
    sectors: List[HistorySeriesPointResponse] = Field(default_factory=list)


class HistorySeriesResponse(BaseModel):
    days: List[HistorySeriesDayResponse] = Field(default_factory=list)


class UpdateSectorPickRequest(BaseModel):
    enabled_keys: List[str]


class ScanSectorPickRequest(BaseModel):
    """Optional body for POST /sector-pick/scan."""

    enabled_keys: Optional[List[str]] = None


class HoldingsPreviewRequest(BaseModel):
    key: str = Field(..., description="Catalog key whose signal_engine holdings to load")


def _catalog_payload() -> list[CatalogItemResponse]:
    return [CatalogItemResponse(**row.annotated_dict()) for row in CATALOG]


def _holding_response(row: HoldingRow) -> HoldingResponse:
    return HoldingResponse(**row.to_dict())


def _result_response(result: SectorPickResult | None) -> SectorPickResultResponse | None:
    if result is None:
        return None
    holdings = result.holdings
    if result.decision is not None:
        holdings = clip_holdings(holdings, result.decision)
    return SectorPickResultResponse(
        as_of=result.as_of,
        scanned_at=result.scanned_at,
        recommended_key=result.recommended_key,
        holdings_key=result.holdings_key,
        stance=result.stance,
        reason=result.reason,
        sectors=[SectorScoreResponse(**row.to_dict()) for row in result.sectors],
        holdings=[_holding_response(row) for row in holdings],
        ops=DayOpsResponse(**result.ops.to_dict()),
        holdings_run_id=result.holdings_run_id,
        provisional=result.provisional,
        market_regime=result.market_regime,
        mom120=result.mom120,
        decision=result.decision.to_dict() if result.decision is not None else None,
        locked_key=result.locked_key,
        map_key=result.map_key,
        challenger_key=result.challenger_key,
        challenger_days=int(result.challenger_days or 0),
        challenger_as_of=result.challenger_as_of,
        lock_event=result.lock_event,
        lock_reason=result.lock_reason,
    )


def _to_response(state: SectorPickState) -> SectorPickResponse:
    return SectorPickResponse(
        enabled_keys=list(state.enabled_keys),
        catalog=_catalog_payload(),
        result=_result_response(state.result),
    )


def register_sector_pick_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    """Register GET/PUT /sector-pick and POST scan/holdings."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if require_auth is None:
        require_auth = host.require_auth

    @app.get(
        "/sector-pick",
        response_model=SectorPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_sector_pick() -> SectorPickResponse:
        return _to_response(load_state())

    @app.put(
        "/sector-pick",
        response_model=SectorPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def put_sector_pick(body: UpdateSectorPickRequest) -> SectorPickResponse:
        try:
            keys = validate_enabled_keys(body.enabled_keys)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state = load_state()
        state.enabled_keys = keys
        save_state(state)
        return _to_response(state)

    @app.post(
        "/sector-pick/scan",
        response_model=SectorPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_sector_pick_scan(
        body: ScanSectorPickRequest = ScanSectorPickRequest(),
    ) -> SectorPickResponse:
        state = load_state()
        if body.enabled_keys is not None:
            try:
                state.enabled_keys = validate_enabled_keys(body.enabled_keys)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            incoming = ThemeLock.from_dict(state.theme_lock)
            scanned = run_sector_pick_scan(state.enabled_keys, theme_lock=incoming)
            state.result = commit_scan_result(scanned)
            # Preview endpoint must not touch theme_lock; official scan is the only writer.
            state.theme_lock = ThemeLock(
                locked_key=state.result.locked_key,
                challenger_key=state.result.challenger_key,
                challenger_days=int(state.result.challenger_days or 0),
                challenger_as_of=state.result.challenger_as_of,
            ).to_dict()
        except SectorPickError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("sector-pick scan failed")
            raise HTTPException(
                status_code=500, detail=f"sector-pick scan failed: {exc}"
            ) from exc
        save_state(state)
        return _to_response(state)

    @app.get(
        "/sector-pick/history",
        response_model=List[HistorySummaryResponse],
        dependencies=[Depends(require_auth)],
    )
    async def get_sector_pick_history(
        limit: int = Query(30, ge=1, le=366),
    ) -> List[HistorySummaryResponse]:
        return [HistorySummaryResponse(**row) for row in list_snapshot_summaries(limit=limit)]

    @app.get(
        "/sector-pick/history/series",
        response_model=HistorySeriesResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_sector_pick_history_series(
        limit: int = Query(30, ge=1, le=366),
    ) -> HistorySeriesResponse:
        """Must be registered before ``/history/{as_of}`` so ``series`` is not a date."""
        return HistorySeriesResponse.model_validate(list_history_series(limit=limit))

    @app.get(
        "/sector-pick/history/{as_of}",
        response_model=SectorPickResultResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_sector_pick_history_day(as_of: str) -> SectorPickResultResponse:
        snap = load_day_snapshot(as_of)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {as_of}")
        payload = _result_response(snap)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {as_of}")
        return payload

    @app.post(
        "/sector-pick/holdings",
        response_model=SectorPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_sector_pick_holdings(body: HoldingsPreviewRequest) -> SectorPickResponse:
        """Reload holdings for one catalog sector without changing the winner."""
        key = str(body.key or "").strip()
        if recipe_by_key(key) is None:
            raise HTTPException(status_code=400, detail=f"unknown sector key: {key!r}")
        state = load_state()
        if state.result is None:
            raise HTTPException(
                status_code=400,
                detail="scan sectors first before previewing holdings",
            )
        try:
            holdings, ops, run_id, provisional = load_sector_holdings(key)
        except SectorPickError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("sector-pick holdings preview failed")
            raise HTTPException(
                status_code=500, detail=f"holdings preview failed: {exc}"
            ) from exc
        state.result.holdings = holdings
        state.result.ops = ops
        state.result.holdings_key = key
        state.result.holdings_run_id = run_id
        state.result.provisional = provisional
        for row in state.result.sectors:
            if row.key == key:
                stamp_book(row, holdings)
                break
        save_state(state)
        return _to_response(state)
