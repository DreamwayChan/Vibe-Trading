"""Data-center HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_data_routes(app)``.

Endpoints (phase 0 — catalog & health, series reads follow in later phases):

``GET  /data/overview``            console header + overview tab in one call
``GET  /data/datasets``            every declared dataset with spec + health
``GET  /data/datasets/{name}``     one dataset: contract, partitions, quality
``GET  /data/quality``             flattened alerts
``POST /data/catalog/rebuild``     rescan manifests (heavy; runs in a thread)

All responses are built from :mod:`src.data.hub`, so the page never has to
guess what "latest" means — ``as_of`` / ``settled`` travel with the payload.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AsOfResponse(BaseModel):
    date: str
    settled: bool
    live: bool = False
    reason: str = ""


class FreshnessResponse(BaseModel):
    name: str
    label: str
    category: str
    status: str
    rows: Optional[int] = None
    symbols: Optional[int] = None
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    lag_days: Optional[int] = None
    files: Optional[int] = None
    bytes: Optional[int] = None
    last_write: Optional[str] = None
    last_source: Optional[str] = None
    consumers: List[str] = Field(default_factory=list)
    retention_days: Optional[int] = None
    sla_minutes: int = 0
    dirty: bool = False


class LakeResponse(BaseModel):
    root: str = ""
    files: int = 0
    bytes: int = 0
    exists: bool = False


class CountsResponse(BaseModel):
    declared: int = 0
    with_data: int = 0
    empty: int = 0
    total_rows: int = 0


class CalendarStatusResponse(BaseModel):
    source: str = "unknown"
    available: bool = False


class QuotaEndpointResponse(BaseModel):
    api: str
    per_token: int = 0
    tokens: int = 0
    remaining: int = 0


class QuotaResponse(BaseModel):
    tokens: int = 0
    endpoints: List[QuotaEndpointResponse] = Field(default_factory=list)
    note: str = ""


class AlertResponse(BaseModel):
    dataset: str
    label: str
    severity: str
    code: str
    message: str


class OverviewResponse(BaseModel):
    as_of: AsOfResponse
    generated_at: str
    strict: bool = False
    lake: LakeResponse
    counts: CountsResponse
    calendar: CalendarStatusResponse
    quota: QuotaResponse
    freshness: List[FreshnessResponse] = Field(default_factory=list)
    alerts: List[AlertResponse] = Field(default_factory=list)


class DatasetCardResponse(BaseModel):
    name: str
    label: str
    category: str
    category_label: str
    keys: List[str] = Field(default_factory=list)
    partitions: List[str] = Field(default_factory=list)
    columns: List[Dict[str, str]] = Field(default_factory=list)
    frequency: str = ""
    markets: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    consumers: List[str] = Field(default_factory=list)
    settle_rule: str = ""
    retention_days: Optional[int] = None
    sla_minutes: int = 0
    ingest: Optional[str] = None
    checks: List[Dict[str, str]] = Field(default_factory=list)
    description: str = ""
    health: FreshnessResponse


class DatasetListResponse(BaseModel):
    as_of: AsOfResponse
    datasets: List[DatasetCardResponse] = Field(default_factory=list)


class PartitionResponse(BaseModel):
    name: str
    rows: Optional[int] = None
    files: int = 0
    bytes: int = 0


class DatasetDetailResponse(BaseModel):
    spec: Dict[str, Any]
    manifest: Optional[Dict[str, Any]] = None
    health: FreshnessResponse
    partitions: List[PartitionResponse] = Field(default_factory=list)
    quality: Dict[str, Any] = Field(default_factory=dict)
    schema_drift: Dict[str, List[str]] = Field(default_factory=dict)


class QualityResponse(BaseModel):
    as_of: AsOfResponse
    alerts: List[AlertResponse] = Field(default_factory=list)


class RebuildRequest(BaseModel):
    datasets: Optional[List[str]] = None


class BarPoint(BaseModel):
    t: str
    o: Optional[float] = None
    h: Optional[float] = None
    l: Optional[float] = None
    c: Optional[float] = None
    v: Optional[float] = None


class SymbolSeries(BaseModel):
    bars: int = 0
    points: List[BarPoint] = Field(default_factory=list)


class BarsResponse(BaseModel):
    as_of: AsOfResponse
    interval: str
    adjust: str = "none"
    derived_from: Optional[str] = None
    sources: List[str] = Field(default_factory=list)
    coverage: Dict[str, Any] = Field(default_factory=dict)
    series: Dict[str, SymbolSeries] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class CoverageBucket(BaseModel):
    key: str
    sessions: int = 0


class CoverageSymbol(BaseModel):
    symbol: str
    covered: int = 0


class CoverageCell(BaseModel):
    symbol: str
    bucket: str
    covered: int = 0
    sessions: int = 0


class CoverageResponse(BaseModel):
    as_of: AsOfResponse
    interval: str
    granularity: str = "session"
    buckets: List[CoverageBucket] = Field(default_factory=list)
    symbols: List[CoverageSymbol] = Field(default_factory=list)
    cells: List[CoverageCell] = Field(default_factory=list)
    truncated: bool = False
    warnings: List[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    """One ingest job.  Optional fields are ingester-specific and filtered."""

    dataset: str = Field(..., description="Declared dataset name, e.g. bars_daily")
    start: Optional[str] = Field(None, description="Inclusive YYYY-MM-DD")
    end: Optional[str] = Field(None, description="Inclusive YYYY-MM-DD")
    symbols: Optional[List[str]] = Field(None, description="Explicit universe")
    limit: Optional[int] = Field(None, ge=1, le=50_000, description="Symbol cap")
    interval: Optional[str] = Field(None, description="Bar interval for intraday datasets")
    universe_mode: Optional[str] = Field(
        None, description="'full' for the whole listed market (lifts the session cap)"
    )
    workers: Optional[int] = Field(None, ge=1, le=16, description="Concurrent fetches")
    refresh: bool = Field(False, description="Re-pull a window the lake already covers")


class IngestJobResponse(BaseModel):
    id: str
    dataset: str
    params: Dict[str, Any] = Field(default_factory=dict)
    status: str
    message: str = ""
    progress: float = 0.0
    queued_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    seconds: Optional[float] = None
    report: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # The resume point, so the console can say "已完成 3,120/5,576" and mean it.
    cursor: Optional[Dict[str, Any]] = None
    rows: int = 0
    done: Optional[int] = None
    total: Optional[int] = None


class IngestDatasetOption(BaseModel):
    name: str
    label: str
    category: str
    ingest: Optional[str] = None
    runnable: bool = False
    reason: str = ""
    params: List[str] = Field(default_factory=list)
    sla_minutes: int = 0
    sources: List[str] = Field(default_factory=list)


class IngestStatusResponse(BaseModel):
    active: List[IngestJobResponse] = Field(default_factory=list)
    jobs: List[IngestJobResponse] = Field(default_factory=list)
    datasets: List[IngestDatasetOption] = Field(default_factory=list)


def _symbol_list(symbols: Optional[str]) -> Optional[List[str]]:
    """Parse ``?symbols=a,b`` into a list; ``None`` means "everything stored"."""
    if not symbols:
        return None
    items = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    return items or None


def register_data_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    """Register the data-center endpoints on ``app``."""
    import sys as _sys

    if require_auth is None:
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        require_auth = host.require_auth

    @app.get(
        "/data/overview",
        response_model=OverviewResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_data_overview(
        live: bool = Query(False, description="Treat today's forming session as the as-of"),
        refresh: bool = Query(False, description="Rescan every manifest before answering"),
    ) -> OverviewResponse:
        """Lake health, freshness per dataset, source quota and alerts."""
        from src.data import hub

        try:
            payload = await asyncio.to_thread(hub.overview, live=live, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - surface a clean 500
            logger.exception("data overview failed")
            raise HTTPException(status_code=500, detail=f"data overview failed: {exc}") from exc
        return OverviewResponse(**payload)

    @app.get(
        "/data/datasets",
        response_model=DatasetListResponse,
        dependencies=[Depends(require_auth)],
    )
    async def list_datasets() -> DatasetListResponse:
        """Every declared dataset with its contract and current health."""
        from src.data import hub

        as_of = await asyncio.to_thread(hub.resolve_as_of)
        cards = await asyncio.to_thread(hub.dataset_cards, as_of)
        return DatasetListResponse(as_of=AsOfResponse(**as_of.to_dict()), datasets=cards)  # type: ignore[arg-type]

    @app.get(
        "/data/datasets/{name}",
        response_model=DatasetDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_dataset(
        name: str,
        refresh: bool = Query(False, description="Rescan this dataset before answering"),
    ) -> DatasetDetailResponse:
        """Contract, partitions, quality checks and schema drift for one dataset."""
        from src.data import hub
        from src.data.registry import REGISTRY

        if name not in REGISTRY:
            raise HTTPException(status_code=404, detail=f"unknown dataset: {name}")
        try:
            payload = await asyncio.to_thread(hub.dataset_detail, name, refresh=refresh)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("dataset detail failed for %s", name)
            raise HTTPException(status_code=500, detail=f"dataset detail failed: {exc}") from exc
        return DatasetDetailResponse(**payload)

    @app.get(
        "/data/bars",
        response_model=BarsResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_bars(
        symbols: Optional[str] = Query(None, description="Comma separated; empty means every stored symbol"),
        interval: str = Query("1d", description="1m/5m/15m/30m/1H/4H/1d"),
        start: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD"),
        end: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD"),
        limit: int = Query(20_000, ge=1, le=200_000, description="Max bars per symbol"),
        adjust: str = Query(
            "none",
            description="none 不复权 / qfq 前复权 / hfq 后复权（由湖内 adj_factor 推导）",
        ),
    ) -> BarsResponse:
        """Bar series for the console chart, with the envelope that labels it."""
        from src.data import hub

        try:
            payload = await asyncio.to_thread(
                hub.bars,
                interval,
                symbols=_symbol_list(symbols),
                start=start,
                end=end,
                limit=limit,
                adjust=adjust,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("bar read failed")
            raise HTTPException(status_code=500, detail=f"bar read failed: {exc}") from exc
        return BarsResponse(**payload)

    @app.get(
        "/data/coverage",
        response_model=CoverageResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_coverage(
        symbols: Optional[str] = Query(None, description="Comma separated; empty means every stored symbol"),
        interval: str = Query("1d"),
        start: Optional[str] = Query(None),
        end: Optional[str] = Query(None),
    ) -> CoverageResponse:
        """Symbol × session presence matrix (auto-bucketed by month when long)."""
        from src.data import hub

        try:
            payload = await asyncio.to_thread(
                hub.coverage_grid,
                interval,
                symbols=_symbol_list(symbols),
                start=start,
                end=end,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("coverage read failed")
            raise HTTPException(status_code=500, detail=f"coverage read failed: {exc}") from exc
        return CoverageResponse(**payload)

    @app.get(
        "/data/quality",
        response_model=QualityResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_quality() -> QualityResponse:
        """Flattened quality/freshness alerts across every dataset."""
        from src.data import hub

        as_of = await asyncio.to_thread(hub.resolve_as_of)
        alerts = await asyncio.to_thread(hub.quality_alerts, as_of)
        return QualityResponse(as_of=AsOfResponse(**as_of.to_dict()), alerts=alerts)  # type: ignore[arg-type]

    @app.post(
        "/data/catalog/rebuild",
        dependencies=[Depends(require_auth)],
    )
    async def rebuild_catalog(body: RebuildRequest | None = None) -> Dict[str, Any]:
        """Rescan manifests — the only slow call in this module."""
        from src.data import catalog

        targets = (body.datasets if body else None) or None
        rebuilt = await asyncio.to_thread(catalog.rebuild_all, targets)
        return {
            "rebuilt": sorted(rebuilt),
            "rows": {name: (payload or {}).get("rows") for name, payload in rebuilt.items()},
        }

    @app.get(
        "/data/ingest",
        response_model=IngestStatusResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_ingest_status(limit: int = Query(20, ge=1, le=100)) -> IngestStatusResponse:
        """Which datasets can be filled, what is running, what ran recently.

        One call so the console does not need a request per dataset to know
        whether its "补齐" button is even meaningful.
        """
        from src.data import ingest

        return IngestStatusResponse(
            active=await asyncio.to_thread(ingest.active),
            jobs=await asyncio.to_thread(ingest.jobs, limit),
            datasets=await asyncio.to_thread(ingest.dataset_options),
        )

    @app.post(
        "/data/ingest",
        response_model=IngestJobResponse,
        status_code=202,
        dependencies=[Depends(require_auth)],
    )
    async def start_ingest(body: IngestRequest) -> IngestJobResponse:
        """Queue one ingest job and return immediately.

        A backfill can take minutes; blocking the request would time out the
        browser and hide the outcome.  The page polls
        ``/data/ingest/jobs/{id}`` (or the status endpoint) instead.
        """
        from src.data import ingest

        params = {
            "start": body.start,
            "end": body.end,
            "symbols": body.symbols,
            "limit": body.limit,
            "interval": body.interval,
            "universe_mode": body.universe_mode,
            "workers": body.workers,
            "refresh": body.refresh or None,
        }
        try:
            payload = await asyncio.to_thread(ingest.submit, body.dataset, **params)
        except ingest.IngestBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ingest.IngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("failed to start ingest for %s", body.dataset)
            raise HTTPException(status_code=500, detail=f"入库启动失败：{exc}") from exc
        return IngestJobResponse(**payload)

    @app.get(
        "/data/ingest/jobs/{job_id}",
        response_model=IngestJobResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_ingest_job(job_id: str) -> IngestJobResponse:
        """One job's status, progress, report or error."""
        from src.data import ingest

        payload = await asyncio.to_thread(ingest.job, job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"未知任务：{job_id}")
        return IngestJobResponse(**payload)

    @app.post(
        "/data/ingest/jobs/{job_id}/cancel",
        response_model=IngestJobResponse,
        dependencies=[Depends(require_auth)],
    )
    async def cancel_ingest_job(job_id: str) -> IngestJobResponse:
        """Ask a job to stop at its next unit boundary.

        Cooperative by design: the ingester finishes the symbol or session it is
        on, flushes, and records the cursor, so cancelling leaves a consistent
        lake and a remainder the next run picks up.  Killing the thread would
        leave a half-merged partition instead.
        """
        from src.data import ingest

        try:
            payload = await asyncio.to_thread(ingest.cancel, job_id)
        except ingest.IngestError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return IngestJobResponse(**payload)
