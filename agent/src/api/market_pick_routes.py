"""Market-pick HTTP routes: cached GET + explicit POST refresh + daily history."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.market_pick.models import MarketPickResult
from src.market_pick.scan import MarketPickError, run_market_pick_scan
from src.market_pick.store import (
    AlreadyScannedToday,
    list_snapshot_summaries,
    load_day_snapshot,
    load_latest_snapshot,
    require_fresh_scan,
    save_day_snapshot,
)

logger = logging.getLogger(__name__)


class BoardScoreResponse(BaseModel):
    bk: str
    name: str
    board_type: str
    classified: int = 0
    universe_size: int = 0
    rs_n: int = 0
    dip_n: int = 0
    knife_n: int = 0
    wait_n: int = 0
    sell_n: int = 0
    rs_share: Optional[float] = None
    knife_share: Optional[float] = None
    live_score: Optional[float] = None
    rank: Optional[int] = None
    ret_20d: Optional[float] = None
    timing_mult: Optional[float] = None
    ew_ma20_ratio: Optional[float] = None
    skip_reason: Optional[str] = None
    catalog_key: Optional[str] = None
    catalog_name: Optional[str] = None


class MarketPickResultResponse(BaseModel):
    as_of: str
    scanned_at: str
    market_regime: Optional[str] = None
    mom120: Optional[float] = None
    stance: str = "none"
    reason: str = ""
    recommended_bk: Optional[str] = None
    map_bk: Optional[str] = None
    unique_symbols: int = 0
    ohlcv_ok: int = 0
    fetch_error_count: int = 0
    boards: List[BoardScoreResponse] = Field(default_factory=list)


class MarketPickResponse(BaseModel):
    result: Optional[MarketPickResultResponse] = None
    cached: bool = True


class ScanMarketPickRequest(BaseModel):
    force: bool = False


class HistorySummaryResponse(BaseModel):
    """One calendar-day row for the history date pills (newest first)."""

    as_of: str
    scanned_at: str
    recommended_bk: Optional[str] = None
    recommended_name: Optional[str] = None
    map_bk: Optional[str] = None
    stance: str = "none"
    market_regime: Optional[str] = None
    board_count: int = 0
    unique_symbols: int = 0
    ohlcv_ok: int = 0
    fetch_error_count: int = 0


def _result_response(result: MarketPickResult | None) -> MarketPickResultResponse | None:
    if result is None:
        return None
    return MarketPickResultResponse.model_validate(result.to_dict())


def register_market_pick_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if require_auth is None:
        require_auth = host.require_auth

    @app.get(
        "/market-pick",
        response_model=MarketPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_market_pick() -> MarketPickResponse:
        """Read the latest daily snapshot. Never hits Eastmoney or Tushare."""
        return MarketPickResponse(
            result=_result_response(load_latest_snapshot()),
            cached=True,
        )

    @app.post(
        "/market-pick/scan",
        response_model=MarketPickResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_market_pick_scan(
        body: ScanMarketPickRequest = ScanMarketPickRequest(),
    ) -> MarketPickResponse:
        as_of = date.today().isoformat()
        try:
            require_fresh_scan(as_of, force=body.force)
        except AlreadyScannedToday as exc:
            raise HTTPException(
                status_code=409,
                detail="today's snapshot already exists; pass force=true to rebuild",
            ) from exc
        try:
            result = run_market_pick_scan()
        except MarketPickError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("market-pick scan failed")
            raise HTTPException(
                status_code=500, detail=f"market-pick scan failed: {exc}"
            ) from exc
        save_day_snapshot(result)
        return MarketPickResponse(result=_result_response(result), cached=False)

    @app.get(
        "/market-pick/history",
        response_model=List[HistorySummaryResponse],
        dependencies=[Depends(require_auth)],
    )
    async def get_market_pick_history(
        limit: int = Query(30, ge=1, le=366),
    ) -> List[HistorySummaryResponse]:
        """Newest-first date pills. Same-day refreshes already collapsed to one file."""
        return [HistorySummaryResponse(**row) for row in list_snapshot_summaries(limit=limit)]

    @app.get(
        "/market-pick/history/{as_of}",
        response_model=MarketPickResultResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_market_pick_history_day(as_of: str) -> MarketPickResultResponse:
        snap = load_day_snapshot(as_of)
        payload = _result_response(snap)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {as_of}")
        return payload
