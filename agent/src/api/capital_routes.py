"""Capital-flow HTTP routes (ETF-OI + national-team flow proxy).

Mounted by ``agent/api_server.py`` via ``register_capital_routes(app)``.

Endpoints
---------
``GET  /capital/etf-oi``      build (or serve cached) ETF-OI report
``GET  /capital/universe``    static tracked-fund metadata + methodology text

The build is network-bound (exchange share files), so results are memoised for
five minutes and the heavy work runs in a worker thread — an ``async`` handler
must never block the event loop for the ~20s a cold build needs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.capital import store
from src.capital.etf_oi import DEFAULT_WINDOW, build_series
from src.capital.sources import CapitalSourceError, clamp_window, describe_sources
from src.capital.universe import universe_meta

logger = logging.getLogger(__name__)

# In-process memo: cold builds touch ~120 exchange endpoints.
_CACHE_TTL_S = 300
_CACHE: dict[tuple[int, str, str], tuple[float, Any]] = {}
_BUILD_LOCK = threading.Lock()


class CapitalPointResponse(BaseModel):
    date: str
    aum: Optional[float] = None
    shares_total: Optional[float] = None
    index_close: Optional[float] = None
    index_mcap: Optional[float] = None
    oi_pct: Optional[float] = None
    net_flow: Optional[float] = None
    nav_pnl: Optional[float] = None
    flow_bp: Optional[float] = None
    fund_count: int = 0
    missing_count: int = 0
    provisional: bool = False


class FundRowResponse(BaseModel):
    code: str
    name: str
    exchange: str = "SH"
    shares: Optional[float] = None
    shares_prev: Optional[float] = None
    shares_change: Optional[float] = None
    nav: Optional[float] = None
    nav_prev: Optional[float] = None
    aum: Optional[float] = None
    aum_prev: Optional[float] = None
    net_flow: Optional[float] = None
    nav_pnl: Optional[float] = None
    group_weight: Optional[float] = None
    stale: bool = False


class FlowStatsResponse(BaseModel):
    oi_pct: Optional[float] = None
    oi_change_bp: Optional[float] = None
    aum: Optional[float] = None
    aum_change: Optional[float] = None
    net_flow: Optional[float] = None
    flow_5d: Optional[float] = None
    flow_20d: Optional[float] = None
    flow_window: Optional[float] = None
    flow_5d_bp: Optional[float] = None
    flow_20d_bp: Optional[float] = None
    flow_z: Optional[float] = None
    inflow_streak: int = 0
    oi_percentile: Optional[float] = None
    regime: str = "neutral"
    score: Optional[float] = None
    observed_days: int = 0
    flow_days: int = 0


class McapResponse(BaseModel):
    date: str = ""
    weights_date: str = ""
    index_close: Optional[float] = None
    per_point: Optional[float] = None
    total: Optional[float] = None
    mode: str = "circ_mv"
    total_circ_mv: Optional[float] = None
    total_weight_implied: Optional[float] = None
    constituents: int = 0
    covered: int = 0


class EtfOiResponse(BaseModel):
    as_of: str = ""
    built_at: str = ""
    window: int = 0
    points: List[CapitalPointResponse] = Field(default_factory=list)
    funds: List[FundRowResponse] = Field(default_factory=list)
    stats: FlowStatsResponse = Field(default_factory=FlowStatsResponse)
    mcap: McapResponse = Field(default_factory=McapResponse)
    methodology: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    provisional: bool = False
    universe_size: int = 0
    cached: bool = False


class FundMetaResponse(BaseModel):
    code: str
    name: str
    exchange: str


class UniverseResponse(BaseModel):
    index_code: str
    index_label: str
    funds: List[FundMetaResponse] = Field(default_factory=list)
    count: int = 0
    note: str = ""
    sources: List[Dict[str, str]] = Field(default_factory=list)
    default_window: int = DEFAULT_WINDOW
    mcap_modes: List[str] = Field(default_factory=lambda: ["circ_mv", "weight_implied"])


def _build_report(window: int, mode: str, codes: Optional[str], refresh: bool) -> Any:
    """Memoised build with a persisted-report fallback."""
    key = (window, mode, (codes or "").strip())
    now = time.time()
    memo = _CACHE.get(key)
    if memo is not None and not refresh and now - memo[0] < _CACHE_TTL_S:
        report = memo[1]
        report.cached = True
        return report

    # Serialise cold builds: six parallel page loads must not hit the exchange
    # 700 times at once.
    with _BUILD_LOCK:
        memo = _CACHE.get(key)
        if memo is not None and not refresh and time.time() - memo[0] < _CACHE_TTL_S:
            memo[1].cached = True
            return memo[1]
        try:
            report = build_series(window=window, mcap_mode=mode, codes=codes)
        except CapitalSourceError as exc:
            stale = store.load_report()
            if stale is None:
                raise
            logger.warning("capital build failed, serving persisted series: %s", exc)
            stale.warnings.append(f"上游数据源暂不可用，展示最近一次成功结果：{exc}")
            return stale
        _CACHE[key] = (time.time(), report)
        store.save_report(report)
        return report


def register_capital_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    """Register the capital-flow endpoints on ``app``."""
    import sys as _sys

    if require_auth is None:
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        require_auth = host.require_auth

    @app.get(
        "/capital/etf-oi",
        response_model=EtfOiResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_etf_oi(
        window: int = Query(DEFAULT_WINDOW, ge=20, le=500, description="Trading days to load"),
        mcap_mode: Literal["circ_mv", "weight_implied"] = Query(
            "circ_mv",
            description="Market-cap denominator: Σ流通市值 or 中证权重反推的自由流通市值",
        ),
        codes: Optional[str] = Query(None, description="Comma separated fund-code override"),
        refresh: bool = Query(False, description="Ignore the 5-minute memo and rebuild"),
    ) -> EtfOiResponse:
        """Headline CSI 300 ETF-OI series plus the daily subscription tape."""
        try:
            report = await asyncio.to_thread(
                _build_report, clamp_window(window), mcap_mode, codes, bool(refresh)
            )
        except CapitalSourceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface a clean 500
            logger.exception("capital etf-oi build failed")
            raise HTTPException(status_code=500, detail=f"capital build failed: {exc}") from exc
        return EtfOiResponse(**report.to_dict())

    @app.get(
        "/capital/universe",
        response_model=UniverseResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_capital_universe() -> UniverseResponse:
        """Tracked fund whitelist, data sources and default parameters."""
        meta = universe_meta()
        return UniverseResponse(
            index_code=str(meta["index_code"]),
            index_label=str(meta["index_label"]),
            funds=[FundMetaResponse(**item) for item in meta["funds"]],  # type: ignore[arg-type]
            count=int(meta["count"]),  # type: ignore[arg-type]
            note=str(meta["note"]),
            sources=describe_sources(),
            default_window=DEFAULT_WINDOW,
        )
