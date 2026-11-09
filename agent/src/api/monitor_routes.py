"""Monitor board HTTP routes (config + deterministic scan + holdings history).

Mounted by ``agent/api_server.py`` via ``register_monitor_routes(app)``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.monitor.models import (
    MAX_SYMBOLS,
    MonitorConfig,
    StrategyRef,
    _normalize_symbols,
)
from src.monitor.forecast import run_monitor_forecast
from src.monitor.scan import MonitorScanError, run_monitor_scan, validate_strategy_ref
from src.monitor.store import (
    board_id_for_strategy,
    clear_board_scan_cache,
    clear_strategy_history,
    is_safe_forecast_id,
    is_safe_history_key,
    list_forecast_summaries,
    list_snapshot_summaries,
    load_board_for_strategy,
    load_day_snapshot,
    load_forecast_snapshot,
    load_monitor,
    save_forecast_snapshot,
    save_monitor,
)
from src.process.attach import clip_holdings

logger = logging.getLogger(__name__)


def _strategy_store():
    """Strategy store for gate stamps. None when the store cannot be opened."""
    from src.monitor.admission import open_strategy_store

    return open_strategy_store()

# Stable id used when the UI enables “daily scan” via scheduled-runs.
MONITOR_SCHEDULE_JOB_ID = "monitor-scan-daily"
# Simplified cron (no ranges): daily 07:30 UTC ≈ 15:30 CST.
MONITOR_SCHEDULE_DEFAULT_CRON = "30 7 * * *"


class StrategyRefBody(BaseModel):
    """Strategy pointer in request bodies."""

    type: Literal["run_dir", "shadow"] = "run_dir"
    run_id: Optional[str] = None
    shadow_id: Optional[str] = None


class UpdateMonitorRequest(BaseModel):
    """Patch the active board, or switch to another strategy's board.

    When ``strategy`` points at a different source, the server loads that
    source's stored board (symbols + last scan) instead of mutating the
    previous board in place. Symbols/schedule in the same request apply only
    to the target board.
    """

    symbols: Optional[List[str]] = Field(
        None, description=f"Tracked symbols for this strategy board (max {MAX_SYMBOLS})"
    )
    strategy: Optional[StrategyRefBody] = None
    schedule: Optional[str] = Field(
        None,
        description="Cron or interval ms; empty string clears. Syncs scheduled-runs when set.",
    )


class ForecastMonitorRequest(BaseModel):
    """Optional body for POST /monitor/forecast."""

    as_of: Optional[str] = Field(
        None,
        description=(
            "History replay date (YYYY-MM-DD). Omit or null uses the live board "
            "holdings and today's bars (with the same-day live overlay)."
        ),
    )


class ForecastBarResponse(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class ForecastMaBundleResponse(BaseModel):
    ma5: List[Optional[float]] = Field(default_factory=list)
    ma10: List[Optional[float]] = Field(default_factory=list)
    ma20: List[Optional[float]] = Field(default_factory=list)


class ForecastMaResponse(BaseModel):
    history: ForecastMaBundleResponse = Field(default_factory=ForecastMaBundleResponse)
    up: ForecastMaBundleResponse = Field(default_factory=ForecastMaBundleResponse)
    down: ForecastMaBundleResponse = Field(default_factory=ForecastMaBundleResponse)


class ForecastTrendPlanResponse(BaseModel):
    """Long-only major-trend action for one holding (optional on old saves)."""

    market_regime: str
    stock_regime: str
    swing: str
    action: str
    fade_rallies: bool
    reason: str


class ForecastTrendBookResponse(BaseModel):
    """CSI 300 structure + the book-level rule that follows from it."""

    market_regime: Optional[str] = None
    mom120: Optional[float] = None
    rule: str = ""


class ForecastAssessmentResponse(BaseModel):
    bias: str = "neutral"
    style_bucket: str = "low"
    ma_structure: str = "mixed"
    analog_count: int = 0
    up_count: int = 0
    down_count: int = 0
    confidence: float = 0.0
    called: Optional[bool] = None
    market_veto: Optional[str] = None


class ForecastItemResponse(BaseModel):
    symbol: str
    weight: float = 0.0
    price: Optional[float] = None
    up_prob: float = 0.5
    down_prob: float = 0.5
    style_score: float = 0.0
    assessment: ForecastAssessmentResponse = Field(default_factory=ForecastAssessmentResponse)
    history: List[ForecastBarResponse] = Field(default_factory=list)
    up_bars: List[ForecastBarResponse] = Field(default_factory=list)
    down_bars: List[ForecastBarResponse] = Field(default_factory=list)
    ma: ForecastMaResponse = Field(default_factory=ForecastMaResponse)
    error: Optional[str] = None
    trend: Optional[ForecastTrendPlanResponse] = None


class ForecastMarketResponse(BaseModel):
    symbol: str = "000300.SH"
    up_prob: float = 0.5
    down_prob: float = 0.5
    style_score: float = 0.0
    confidence: float = 0.0
    bias: str = "neutral"
    called: bool = False
    # Strong barometer only: lopsided + confident + |10d analog| large enough.
    decisive: bool = False
    impact: float = 0.0
    analog_count: int = 0
    error: Optional[str] = None
    regime: Optional[str] = None
    mom120: Optional[float] = None


class ForecastResponse(BaseModel):
    as_of: Optional[str] = None
    horizon: int = 10
    holding_count: int = 0
    evaluated_count: int = 0
    capped: bool = False
    bias: str = "neutral"
    called_count: int = 0
    market: Optional[ForecastMarketResponse] = None
    trend: Optional[ForecastTrendBookResponse] = None
    items: List[ForecastItemResponse] = Field(default_factory=list)
    decision: Optional[Dict[str, Any]] = None


class ForecastSaveSummaryResponse(BaseModel):
    id: str
    saved_at: str
    run_id: str
    as_of: Optional[str] = None
    bias: str = "neutral"
    holding_count: int = 0
    evaluated_count: int = 0


class ForecastSaveDetailResponse(ForecastResponse):
    id: str
    saved_at: str
    run_id: str


class ScanMonitorRequest(BaseModel):
    """Optional body for POST /monitor/scan."""

    refresh_report: bool = Field(
        False,
        description=(
            "When true, also re-bench the bound run through today so the "
            "strategy report (K-line / buy-sell markers) updates. Default "
            "false keeps legacy scan-only behaviour."
        ),
    )


class SuggestionResponse(BaseModel):
    symbol: str
    action: str
    reason: str
    as_of: str
    position: str = "unknown"
    data_as_of: Optional[str] = None
    price: Optional[float] = None
    provisional: bool = False
    confidence: Optional[float] = None
    signal: Optional[float] = None
    tradable: bool = False
    block_reasons: List[str] = Field(default_factory=list)
    risk_flags: List[str] = Field(default_factory=list)


class HoldingResponse(BaseModel):
    symbol: str
    weight: float
    action: str = "hold"
    price: Optional[float] = None
    data_as_of: Optional[str] = None
    provisional: bool = False
    prev_weight: Optional[float] = None
    # Dual-track tape (hold / buy_dip / reduce). Missing on older snapshots.
    trend: Optional[ForecastTrendPlanResponse] = None


class DayOpsResponse(BaseModel):
    entered: List[str] = Field(default_factory=list)
    exited: List[str] = Field(default_factory=list)
    held: List[str] = Field(default_factory=list)


class MonitorResponse(BaseModel):
    symbols: List[str]
    strategy: StrategyRefBody
    last_scan_at: Optional[str] = None
    suggestions: List[SuggestionResponse] = Field(default_factory=list)
    holdings: List[HoldingResponse] = Field(default_factory=list)
    ops: DayOpsResponse = Field(default_factory=DayOpsResponse)
    schedule: Optional[str] = None
    decision: Optional[Dict[str, Any]] = None
    tradable: bool = False
    tradable_reasons: List[str] = Field(default_factory=list)
    validation_gate: Optional[Dict[str, Any]] = None
    decision_signal: Optional[Dict[str, Any]] = None
    bars_source: Optional[str] = None
    # Held symbols with no tradable, fresh, non-invalidated journal entry.
    journal_alerts: List[str] = Field(default_factory=list)


class RecordSignalRequest(BaseModel):
    """Journal fields for a monitor signal. Missing invalidation downgrades.

    ``strict`` rejects the write (HTTP 422) instead of storing a non-tradable
    row. A blank ``symbol`` covers the whole bound strategy.
    """

    hypothesis_id: Optional[str] = None
    thesis: Optional[str] = None
    observation: Optional[str] = None
    hypothesis: Optional[str] = None
    logic: Optional[str] = None
    validation_plan: Optional[str] = None
    invalidation: Optional[str] = None
    invalidation_kind: Optional[str] = None
    invalidation_side: Optional[str] = None
    invalidation_price: Optional[float] = None
    invalidation_level: Optional[float] = None
    planned_position: Optional[str] = None
    planned_risk: Optional[str] = None
    timestamp: Optional[str] = None
    symbol: Optional[str] = None
    strict: bool = False


class JournalEntryResponse(BaseModel):
    signal_id: str
    timestamp: str
    run_id: str
    hypothesis_id: str = ""
    thesis: str = ""
    observation: str = ""
    hypothesis: str = ""
    logic: str = ""
    validation_plan: str = ""
    invalidation: str = ""
    invalidation_kind: str = ""
    invalidation_side: str = ""
    invalidation_level: Optional[float] = None
    invalidation_price: Optional[float] = None
    planned_position: str = ""
    planned_risk: str = ""
    symbol: str = ""
    tradable: bool = False
    block_reasons: List[str] = Field(default_factory=list)
    outcome: Optional[Dict[str, Any]] = None
    # Recomputed on read, not the value stored at write time: a row keeps its
    # ``tradable`` snapshot forever, but expiry depends on "now". Without these
    # two fields the history list would still say "tradable" for a row the
    # admission gate is already rejecting as ``stale_journal``.
    stale: bool = False
    age_days: Optional[int] = None


class OutcomeRequest(BaseModel):
    """Realised result for one journal row. ``pnl_pct`` is a percent, not a fraction."""

    pnl_pct: float
    note: str = ""


class OutcomeSummaryResponse(BaseModel):
    reviewed: int = 0
    wins: int = 0
    win_rate: Optional[float] = None
    mean_pnl_pct: Optional[float] = None


class HistorySummaryResponse(BaseModel):
    as_of: str
    scanned_at: str
    run_id: str
    provisional: bool = False
    holding_count: int = 0
    entered_count: int = 0
    exited_count: int = 0
    held_count: int = 0
    universe_size: int = 0
    fetch_error_count: int = 0
    live_quote_ok: int = 0
    live_quote_total: int = 0
    live_quote_coverage: float = 0.0
    bars_source: Optional[str] = None
    tradable: bool = False
    validation_passed: bool = False


class HistoryDetailResponse(BaseModel):
    as_of: str
    scanned_at: str
    run_id: str
    provisional: bool = False
    holdings: List[HoldingResponse] = Field(default_factory=list)
    ops: DayOpsResponse = Field(default_factory=DayOpsResponse)
    suggestions: List[SuggestionResponse] = Field(default_factory=list)
    universe_size: int = 0
    fetch_error_count: int = 0
    live_quote_ok: int = 0
    live_quote_total: int = 0
    live_quote_coverage: float = 0.0
    bars_source: Optional[str] = None
    tradable: bool = False
    tradable_reasons: List[str] = Field(default_factory=list)
    validation_gate: Optional[Dict[str, Any]] = None
    decision_signal: Optional[Dict[str, Any]] = None
    decision: Optional[Dict[str, Any]] = None


def _display_holdings(holdings, decision):
    """Clip engine weights for the client; disk snapshots stay unclipped."""
    if decision is None:
        return holdings
    return clip_holdings(holdings, decision)


def _journal_alerts(config: MonitorConfig) -> List[str]:
    from src.monitor.admission import holdings_without_journal, journal_key_for

    try:
        return holdings_without_journal(journal_key_for(config.strategy), list(config.holdings))
    except Exception:  # noqa: BLE001 — alerts are advisory
        logger.debug("journal holding alerts failed", exc_info=True)
        return []


def _to_response(config: MonitorConfig) -> MonitorResponse:
    holdings = _display_holdings(config.holdings, config.decision)
    return MonitorResponse(
        symbols=list(config.symbols),
        strategy=StrategyRefBody(
            type=config.strategy.type,
            run_id=config.strategy.run_id,
            shadow_id=config.strategy.shadow_id,
        ),
        last_scan_at=config.last_scan_at,
        suggestions=[
            SuggestionResponse(**item.to_dict()) for item in config.suggestions
        ],
        holdings=[HoldingResponse(**item.to_dict()) for item in holdings],
        ops=DayOpsResponse(**config.ops.to_dict()),
        schedule=config.schedule,
        decision=config.decision.to_dict() if config.decision is not None else None,
        tradable=config.tradable,
        tradable_reasons=list(config.tradable_reasons),
        validation_gate=config.validation_gate,
        decision_signal=config.decision_signal,
        bars_source=config.bars_source,
        journal_alerts=_journal_alerts(config),
    )


def _apply_update(config: MonitorConfig, body: UpdateMonitorRequest) -> MonitorConfig:
    """Apply a PUT body, switching board documents when the strategy changes."""
    if body.strategy is not None:
        next_strategy = StrategyRef(
            type=body.strategy.type,
            run_id=(body.strategy.run_id or None),
            shadow_id=(body.strategy.shadow_id or None),
        )
        # Allow saving a partial draft (symbols first); scan still validates.
        has_pointer = bool(next_strategy.run_id or next_strategy.shadow_id)
        if has_pointer:
            validate_strategy_ref(next_strategy)
        # Different strategy source → load that board (or empty), do not carry
        # over the previous source's watchlist / suggestions / holdings.
        if board_id_for_strategy(next_strategy) != board_id_for_strategy(config.strategy):
            config = load_board_for_strategy(next_strategy)
        else:
            config.strategy = next_strategy

    if body.symbols is not None:
        config.symbols = _normalize_symbols(body.symbols)
    if body.schedule is not None:
        # Empty string explicitly clears the schedule.
        text = str(body.schedule).strip()
        if text:
            from src.scheduled_research.models import validate_schedule

            try:
                validate_schedule(text)
            except ValueError as exc:
                raise MonitorScanError(str(exc)) from exc
            config.schedule = text
        else:
            config.schedule = None
    return config


def _sync_scheduled_job(config: MonitorConfig) -> None:
    """Best-effort mirror of monitor.schedule into scheduled-runs store.

    Jobs are recorded even when the executor is off, so enabling
    ``VIBE_TRADING_ENABLE_SCHEDULER`` later picks them up. Dispatch for
    ``job_type=monitor_scan`` runs the deterministic scan (not an LLM session).
    """
    try:
        import time

        from src.api.scheduled_routes import _get_scheduled_research_store
        from src.scheduled_research.models import JobStatus, ScheduledResearchJob
    except Exception as exc:  # noqa: BLE001
        logger.debug("scheduled-research unavailable for monitor sync: %s", exc)
        return

    store = _get_scheduled_research_store()
    try:
        existing = store.get(MONITOR_SCHEDULE_JOB_ID)
    except Exception:
        existing = None

    if not config.schedule:
        if existing is not None:
            try:
                store.delete(MONITOR_SCHEDULE_JOB_ID)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to clear monitor schedule job: %s", exc)
        return

    now_ms = int(time.time() * 1000)
    job = ScheduledResearchJob(
        id=MONITOR_SCHEDULE_JOB_ID,
        prompt=(
            "Run the signal monitor board scan now and summarize buy/sell/hold "
            "suggestions for the configured universe."
        ),
        schedule=config.schedule,
        next_run_at=existing.next_run_at if existing is not None else now_ms,
        status=JobStatus.PENDING,
        created_at=existing.created_at if existing is not None else now_ms,
        config={"job_type": "monitor_scan"},
    )
    try:
        store.upsert(job)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to sync monitor schedule job: %s", exc)


def _history_run_id(explicit: Optional[str] = None) -> str:
    """Resolve which strategy history folder to read."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    config = load_monitor()
    return _history_key_for_config(config)


def _history_key_for_config(config: MonitorConfig) -> str:
    """Filesystem key under ``monitor/history/`` for this board's strategy."""
    run_id = (config.strategy.run_id or "").strip()
    if run_id:
        return run_id
    shadow_id = (config.strategy.shadow_id or "").strip()
    if shadow_id:
        return f"shadow_{shadow_id}"
    raise HTTPException(status_code=400, detail="no strategy run_id configured")


def register_monitor_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    """Register GET/PUT /monitor, POST /monitor/scan|/reset|/forecast, forecast saves, and history routes."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if require_auth is None:
        require_auth = host.require_auth

    @app.get("/monitor", response_model=MonitorResponse, dependencies=[Depends(require_auth)])
    async def get_monitor() -> MonitorResponse:
        return _to_response(load_monitor())

    @app.put("/monitor", response_model=MonitorResponse, dependencies=[Depends(require_auth)])
    async def put_monitor(body: UpdateMonitorRequest) -> MonitorResponse:
        config = load_monitor()
        try:
            config = _apply_update(config, body)
        except MonitorScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_monitor(config)
        _sync_scheduled_job(config)
        return _to_response(config)

    @app.post(
        "/monitor/scan",
        response_model=MonitorResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_scan(
        body: ScanMonitorRequest = ScanMonitorRequest(),
    ) -> MonitorResponse:
        config = load_monitor()
        try:
            validate_strategy_ref(config.strategy)
            config = run_monitor_scan(
                config,
                refresh_report=bool(body.refresh_report),
                strategy_store=_strategy_store(),
            )
        except MonitorScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("monitor scan failed")
            raise HTTPException(status_code=500, detail=f"monitor scan failed: {exc}") from exc
        save_monitor(config)
        return _to_response(config)

    @app.post(
        "/monitor/signal",
        response_model=MonitorResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_signal(body: RecordSignalRequest) -> MonitorResponse:
        """Record a decision-journal signal for the bound strategy.

        Missing any of the five hypothesis fields (or thesis, position, risk)
        is stored as ``tradable=false``. ``strict=true`` returns 422 and does
        not write.
        The validation gate is re-evaluated against the bound run; a strategy
        still cannot enter the tradable pool without a passing artifact.
        """
        from src.api.helpers import RUNS_DIR
        from src.hypotheses.journal import DecisionJournal
        from src.monitor.admission import apply_admission, journal_key_for

        config = load_monitor()
        run_key = journal_key_for(config.strategy)
        if not run_key:
            raise HTTPException(
                status_code=400,
                detail="bind a strategy before recording a decision",
            )
        if config.strategy.type == "run_dir":
            try:
                validate_strategy_ref(config.strategy)
            except MonitorScanError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            DecisionJournal().record(
                run_id=run_key,
                hypothesis_id=body.hypothesis_id or "",
                thesis=body.thesis or "",
                observation=body.observation or "",
                hypothesis=body.hypothesis or "",
                logic=body.logic or "",
                validation_plan=body.validation_plan or "",
                invalidation=body.invalidation or "",
                invalidation_kind=body.invalidation_kind or "",
                invalidation_side=body.invalidation_side or "",
                invalidation_price=body.invalidation_price,
                invalidation_level=body.invalidation_level,
                planned_position=body.planned_position or "",
                planned_risk=body.planned_risk or "",
                timestamp=body.timestamp or "",
                symbol=body.symbol or "",
                strict=bool(body.strict),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_dir = None
        if config.strategy.type == "run_dir" and (config.strategy.run_id or "").strip():
            run_dir = RUNS_DIR / config.strategy.run_id.strip()
        try:
            apply_admission(
                config,
                run_key=run_key,
                run_dir=run_dir,
                bars_source=config.bars_source,
                strategy_store=_strategy_store(),
            )
        except Exception as exc:  # noqa: BLE001 — journal row is already durable
            logger.warning("monitor signal admission failed: %s", exc)
            config.tradable = False
            config.tradable_reasons = ["admission_error"]
        save_monitor(config)
        return _to_response(config)

    @app.get(
        "/monitor/journal",
        response_model=List[JournalEntryResponse],
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_journal(
        symbol: Optional[str] = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> List[JournalEntryResponse]:
        """Decision-journal history (newest first) for the bound strategy."""
        from src.hypotheses.journal import DecisionJournal, is_stale, signal_age_days
        from src.monitor.admission import journal_key_for

        run_key = journal_key_for(load_monitor().strategy)
        if not run_key:
            return []
        try:
            rows = DecisionJournal().history(run_key, symbol=symbol or "", limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        entries: List[JournalEntryResponse] = []
        for row in rows:
            payload = row.to_dict()
            # ``tradable`` is the write-time snapshot; expiry is judged against
            # the current clock so the list cannot contradict the board.
            payload["stale"] = is_stale(row)
            payload["age_days"] = signal_age_days(row)
            entries.append(JournalEntryResponse(**payload))
        return entries

    @app.get(
        "/monitor/journal/summary",
        response_model=OutcomeSummaryResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_journal_summary() -> OutcomeSummaryResponse:
        """Reviewed count, win rate, and mean PnL for the bound strategy."""
        from src.hypotheses.journal import DecisionJournal, outcome_summary
        from src.monitor.admission import journal_key_for

        run_key = journal_key_for(load_monitor().strategy)
        if not run_key:
            return OutcomeSummaryResponse()
        try:
            rows = DecisionJournal().history(run_key, limit=500)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return OutcomeSummaryResponse(**outcome_summary(rows))

    @app.post(
        "/monitor/journal/{signal_id}/outcome",
        response_model=JournalEntryResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_journal_outcome(
        signal_id: str, body: OutcomeRequest
    ) -> JournalEntryResponse:
        """Record the realised percent result of one journal entry."""
        from src.hypotheses.journal import DecisionJournal, is_stale, signal_age_days

        try:
            row = DecisionJournal().record_outcome(signal_id, pnl_pct=body.pnl_pct, note=body.note)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"journal entry not found: {signal_id}") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = row.to_dict()
        payload["stale"] = is_stale(row)
        payload["age_days"] = signal_age_days(row)
        return JournalEntryResponse(**payload)

    @app.post(
        "/monitor/reset",
        response_model=MonitorResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_reset() -> MonitorResponse:
        """Wipe this strategy's scan history and rescan from today as day one.

        Keeps symbols / strategy pointer / schedule. After reset there is no
        prior holdings baseline, so the fresh scan treats every current name
        as ``enter``.
        """
        config = load_monitor()
        try:
            validate_strategy_ref(config.strategy)
            history_key = _history_key_for_config(config)
            if not is_safe_history_key(history_key):
                raise MonitorScanError(f"invalid history key: {history_key!r}")
            clear_strategy_history(history_key)
            config = clear_board_scan_cache(config)
            save_monitor(config)
            # Rebuild today's book with an empty prior — new holdings timeline.
            config = run_monitor_scan(config, strategy_store=_strategy_store())
        except MonitorScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("monitor reset failed")
            raise HTTPException(status_code=500, detail=f"monitor reset failed: {exc}") from exc
        save_monitor(config)
        return _to_response(config)

    @app.post(
        "/monitor/forecast",
        response_model=ForecastResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_forecast(
        body: ForecastMonitorRequest = ForecastMonitorRequest(),
    ) -> ForecastResponse:
        """Scenario paths for current (or replay-day) strategy holdings.

        Statistical analog overlay — not a point-price model and not persisted
        into scan history.
        """
        config = load_monitor()
        try:
            validate_strategy_ref(config.strategy)
            report = run_monitor_forecast(config, as_of=body.as_of)
        except MonitorScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("monitor forecast failed")
            raise HTTPException(
                status_code=500, detail=f"monitor forecast failed: {exc}"
            ) from exc
        return ForecastResponse(**report)

    @app.post(
        "/monitor/forecast/saves",
        response_model=ForecastSaveDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    async def post_monitor_forecast_save(
        body: ForecastResponse,
    ) -> ForecastSaveDetailResponse:
        """Keep a copy of the currently displayed forecast so it can be replayed."""
        config = load_monitor()
        try:
            validate_strategy_ref(config.strategy)
            key = _history_key_for_config(config)
            if not is_safe_history_key(key):
                raise MonitorScanError(f"invalid history key: {key!r}")
            if not body.items:
                raise MonitorScanError("cannot save an empty forecast")
            stored = save_forecast_snapshot(key, body.model_dump())
        except MonitorScanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("monitor forecast save failed")
            raise HTTPException(
                status_code=500, detail=f"monitor forecast save failed: {exc}"
            ) from exc
        return ForecastSaveDetailResponse(**stored)

    @app.get(
        "/monitor/forecast/saves",
        response_model=List[ForecastSaveSummaryResponse],
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_forecast_saves(
        limit: int = Query(50, ge=1, le=100),
        run_id: Optional[str] = Query(None, description="Override strategy run id"),
    ) -> List[ForecastSaveSummaryResponse]:
        key = _history_run_id(run_id)
        try:
            rows = list_forecast_summaries(key, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [ForecastSaveSummaryResponse(**row) for row in rows]

    @app.get(
        "/monitor/forecast/saves/{snapshot_id}",
        response_model=ForecastSaveDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_forecast_save(
        snapshot_id: str,
        run_id: Optional[str] = Query(None, description="Override strategy run id"),
    ) -> ForecastSaveDetailResponse:
        if not is_safe_forecast_id(snapshot_id):
            raise HTTPException(status_code=400, detail=f"invalid forecast id: {snapshot_id}")
        key = _history_run_id(run_id)
        snap = load_forecast_snapshot(key, snapshot_id)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"no forecast save {snapshot_id}")
        return ForecastSaveDetailResponse(**snap)

    @app.get(
        "/monitor/history",
        response_model=List[HistorySummaryResponse],
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_history(
        limit: int = Query(30, ge=1, le=366),
        run_id: Optional[str] = Query(None, description="Override strategy run id"),
    ) -> List[HistorySummaryResponse]:
        key = _history_run_id(run_id)
        try:
            rows = list_snapshot_summaries(key, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [HistorySummaryResponse(**row) for row in rows]

    @app.get(
        "/monitor/history/{as_of}",
        response_model=HistoryDetailResponse,
        dependencies=[Depends(require_auth)],
    )
    async def get_monitor_history_day(
        as_of: str,
        run_id: Optional[str] = Query(None, description="Override strategy run id"),
    ) -> HistoryDetailResponse:
        key = _history_run_id(run_id)
        snap = load_day_snapshot(key, as_of)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"no snapshot for {as_of}")
        holdings = _display_holdings(snap.holdings, snap.decision)
        return HistoryDetailResponse(
            as_of=snap.as_of,
            scanned_at=snap.scanned_at,
            run_id=snap.run_id,
            provisional=snap.provisional,
            holdings=[HoldingResponse(**h.to_dict()) for h in holdings],
            ops=DayOpsResponse(**snap.ops.to_dict()),
            suggestions=[SuggestionResponse(**s.to_dict()) for s in snap.suggestions],
            universe_size=snap.universe_size,
            fetch_error_count=snap.fetch_error_count,
            live_quote_ok=snap.live_quote_ok,
            live_quote_total=snap.live_quote_total,
            live_quote_coverage=snap.live_quote_coverage,
            bars_source=snap.bars_source,
            tradable=snap.tradable,
            tradable_reasons=list(snap.tradable_reasons),
            validation_gate=snap.validation_gate,
            decision_signal=snap.decision_signal,
            decision=snap.decision.to_dict() if snap.decision is not None else None,
        )
