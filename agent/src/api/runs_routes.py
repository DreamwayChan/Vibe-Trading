"""Run listing and detail HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_runs_routes(app, ...)``.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from src.api.models import UpdateRunRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions (module-level; host Pydantic models resolved via sys.modules)
# ---------------------------------------------------------------------------


def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON from disk if present."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _load_csv_to_dict(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load CSV rows into a list of dictionaries."""
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        if limit is not None:
            rows = rows[:limit]
        return rows
    except Exception:
        return []


def _run_response_payload(response: Any) -> Dict[str, Any]:
    """Return a JSON-ready payload for opt-in run response variants."""
    return response.model_dump(mode="json")


def _build_response_from_run_dir(
    run_dir: Path,
    elapsed: float,
    *,
    include_analysis: bool = False,
    chart_symbol: Optional[str] = None,
    chart_payload: str = "full",
    chart_symbols_out: Optional[List[str]] = None,
):
    """Build a run response from a persisted run directory."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    RunResponse = host.RunResponse
    BacktestMetrics = host.BacktestMetrics
    RAGSelection = host.RAGSelection
    Artifact = host.Artifact

    run_id = run_dir.name

    response = RunResponse(
        status="unknown",
        run_id=run_id,
        elapsed_seconds=elapsed,
        run_directory=str(run_dir),
    )

    state_data = _load_json_file(run_dir / "state.json")
    if state_data:
        state_status = str(state_data.get("status") or "").lower()
        if state_status == "success":
            response.status = "success"
        elif state_status == "failed":
            response.status = "failed"
            response.reason = state_data.get("reason", "")
        else:
            response.status = state_status or "unknown"
    else:
        response.status = "unknown"

    planner_path = run_dir / "planner_output.json"
    response.planner_output = _load_json_file(planner_path)

    design_path = run_dir / "design_spec.json"
    response.strategy_spec = _load_json_file(design_path)

    rag_path = run_dir / "rag_metadata.json"
    rag_data = _load_json_file(rag_path)
    if rag_data:
        response.rag_selection = RAGSelection(
            selected_api=rag_data.get("selected_api") or rag_data.get("api_code", ""),
            selected_name=rag_data.get("selected_name") or rag_data.get("api_name", ""),
            selected_score=float(rag_data.get("selected_score") or rag_data.get("score", 0.0)),
        )

    metrics_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_path.exists():
        metrics_dict_list = _load_csv_to_dict(metrics_path, limit=1)
        if metrics_dict_list:
            row = metrics_dict_list[0]
            try:
                # Pass ALL CSV columns to BacktestMetrics (extra="allow")
                parsed: dict = {}
                for k, v in row.items():
                    if not k or not v:
                        continue
                    try:
                        parsed[k] = int(float(v)) if k == "trade_count" or k == "max_consecutive_loss" else float(v)
                    except (ValueError, TypeError):
                        continue
                if "final_value" in parsed:
                    response.metrics = BacktestMetrics(**parsed)
            except (ValueError, TypeError):
                pass

    artifacts_dir = run_dir / "artifacts"
    if artifacts_dir.exists():
        for file_path in artifacts_dir.iterdir():
            if file_path.is_file():
                file_type = file_path.suffix.lstrip(".")
                response.artifacts.append(
                    Artifact(
                        name=file_path.name,
                        path=str(file_path),
                        type=file_type if file_type else "unknown",
                        size=file_path.stat().st_size,
                        exists=True,
                    )
                )

    equity_path = run_dir / "artifacts" / "equity.csv"
    if equity_path.exists():
        response.artifacts_equity_csv = _load_csv_to_dict(equity_path)

    metrics_csv_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_csv_path.exists():
        response.artifacts_metrics_csv = _load_csv_to_dict(metrics_csv_path)

    run_card_path = run_dir / "run_card.json"
    if run_card_path.exists():
        try:
            response.run_card = json.loads(run_card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    llm_usage_path = run_dir / "llm_usage.json"
    if llm_usage_path.exists():
        try:
            response.llm_usage = json.loads(llm_usage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    trades_path = run_dir / "artifacts" / "trades.csv"
    if trades_path.exists():
        response.artifacts_trades_csv = _load_csv_to_dict(trades_path)

    validation_path = run_dir / "artifacts" / "validation.json"
    if validation_path.exists():
        try:
            response.validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if response.artifacts_equity_csv:
        filtered_equity = []
        for row in response.artifacts_equity_csv[:1000]:
            filtered_row: Dict[str, Any] = {}
            if "timestamp" in row:
                filtered_row["time"] = row["timestamp"]
            if "equity" in row:
                filtered_row["equity"] = row["equity"]
            if "drawdown" in row:
                filtered_row["drawdown"] = row["drawdown"]
            filtered_equity.append(filtered_row)
        response.equity_curve = filtered_equity

    if response.artifacts_trades_csv:
        response.trade_log = response.artifacts_trades_csv[:500]

    if include_analysis:
        from src.ui_services import build_run_analysis

        analysis = build_run_analysis(
            run_dir,
            symbols=[chart_symbol] if chart_symbol else None,
            include_payload=chart_payload != "summary" or bool(chart_symbol),
            include_symbol_list=chart_symbols_out is not None,
        )
        if chart_symbols_out is not None:
            chart_symbols_out.extend(analysis.get("chart_symbols") or [])
        response.run_stage = analysis.get("run_stage")
        response.run_context = analysis.get("run_context")
        response.price_series = analysis.get("price_series")
        response.indicator_series = analysis.get("indicator_series")
        response.trade_markers = analysis.get("trade_markers")
        response.run_logs = analysis.get("run_logs")

    # Attribution + strategy-fit from the FULL trade list (trade_log is only a
    # preview). Computed after analysis so run_context codes can feed the
    # universe-coverage check. Historical runs never persisted by_symbol.
    if response.artifacts_trades_csv:
        from src.api.run_report import build_run_report_extras

        metrics_dict: Dict[str, Any] = (
            response.metrics.model_dump() if response.metrics is not None else {}
        )
        extras = build_run_report_extras(
            trade_rows=response.artifacts_trades_csv,
            metrics=metrics_dict,
            validation=response.validation,
            config=_load_json_file(run_dir / "config.json"),
            run_card=response.run_card,
            strategy_spec=response.strategy_spec,
            run_context=response.run_context,
        )
        response.by_symbol = extras["by_symbol"]
        response.by_exit_reason = extras["by_exit_reason"]
        response.strategy_assessment = extras["strategy_assessment"]

    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_runs_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the runs routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_runs_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )

    if require_auth is None:
        require_auth = host.require_auth

    # Late-access closures for shared host symbols (monkeypatch-safe)
    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h._validate_path_param(value, kind)

    def _host_RUNS_DIR() -> Path:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h.RUNS_DIR

    def _host_SESSIONS_DIR() -> Path:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        return h.SESSIONS_DIR

    # Pydantic models for response_model (resolved at registration time)
    RunResponse = host.RunResponse
    RunInfo = host.RunInfo

    # --- Routes ---

    @app.get("/runs/{run_id}/code", dependencies=[Depends(require_auth)])
    async def get_run_code(run_id: str):
        """Return strategy source files for a run.

        Args:
            run_id: Run identifier.

        Returns:
            Map filename -> source text.
        """
        _host_validate_path_param(run_id, "run_id")
        run_dir = _host_RUNS_DIR() / run_id / "code"
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail=f"Code directory for run {run_id} not found")
        result = {}
        for f in ["signal_engine.py"]:
            p = run_dir / f
            if p.exists():
                result[f] = p.read_text(encoding="utf-8")
        return result

    @app.get("/runs/{run_id}/pine", dependencies=[Depends(require_auth)])
    async def get_run_pine(run_id: str):
        """Return Pine Script file for a run.

        Args:
            run_id: Run identifier.

        Returns:
            Object with pine script content and exists flag.
        """
        _host_validate_path_param(run_id, "run_id")
        pine_path = _host_RUNS_DIR() / run_id / "artifacts" / "strategy.pine"
        if not pine_path.exists():
            return {"exists": False, "content": None}
        return {
            "exists": True,
            "content": pine_path.read_text(encoding="utf-8"),
        }

    @app.get("/runs/{run_id}", response_model=RunResponse, dependencies=[Depends(require_auth)])
    async def get_run_result(
        run_id: str,
        chart_symbol: Optional[str] = Query(None, description="Opt in to chart payloads for a single symbol"),
        chart_payload: Optional[str] = Query(
            None,
            description="Optional chart payload mode. Use 'summary' to omit chart rows and trade markers.",
        ),
    ):
        """Fetch details for a historical run by ``run_id``.

        The default response stays unchanged for existing consumers. Chart-heavy
        optimizations are opt-in via query parameters.
        """
        _host_validate_path_param(run_id, "run_id")
        if chart_payload not in (None, "summary"):
            raise HTTPException(status_code=400, detail="invalid chart_payload")
        run_dir = _host_RUNS_DIR() / run_id

        if not run_dir.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found"
            )

        wants_chart_meta = bool(chart_payload or chart_symbol)
        chart_symbols: List[str] = []
        response = _build_response_from_run_dir(
            run_dir,
            elapsed=0.0,
            include_analysis=True,
            chart_symbol=chart_symbol,
            chart_payload=chart_payload or "full",
            chart_symbols_out=chart_symbols if wants_chart_meta else None,
        )

        if wants_chart_meta:
            payload = _run_response_payload(response)
            payload["chart_symbols"] = chart_symbols
            return JSONResponse(payload)

        return response

    @app.get("/runs", response_model=List[RunInfo], dependencies=[Depends(require_auth)])
    async def list_runs(
        limit: int = 20,
        strategy_ready: bool = Query(
            False,
            description=(
                "When true, only return runs that have code/signal_engine.py "
                "(Monitor strategy-source picker)."
            ),
        ),
    ):
        """List recent runs with summary fields.

        Chat-only / research sessions often create a run dir without a
        SignalEngine. Pass ``strategy_ready=true`` so Monitor (and similar
        pickers) never offer those non-backtest conversations as strategy
        sources.
        """
        from src.session.store import SessionStore
        from src.ui_services import load_run_context

        limit = min(max(1, limit), 100)
        runs_dir = _host_RUNS_DIR()

        if not runs_dir.exists():
            return []

        run_dirs = sorted(
            [d for d in runs_dir.iterdir() if d.is_dir()],
            key=lambda x: x.name,
            reverse=True
        )

        # Resolve display titles from the linked chat session when present.
        # Sidebar rename updates session.title; req.json.prompt stays immutable.
        session_store = SessionStore(base_dir=_host_SESSIONS_DIR())
        session_title_cache: Dict[str, Optional[str]] = {}

        def _session_title(session_id: str) -> Optional[str]:
            """Return the current session title, cached per list_runs call."""
            if session_id in session_title_cache:
                return session_title_cache[session_id]
            title: Optional[str] = None
            try:
                session = session_store.get_session(session_id)
                if session is not None:
                    cleaned = (session.title or "").strip()
                    title = cleaned or None
            except Exception:
                title = None
            session_title_cache[session_id] = title
            return title

        results = []
        # When filtering for Monitor, keep scanning past non-strategy dirs so
        # ``limit`` still means "up to N usable strategy sources", not "N
        # newest dirs of any kind".
        for d in run_dirs:
            if len(results) >= limit:
                break

            run_id = d.name
            has_signal_engine = (d / "code" / "signal_engine.py").is_file()
            if strategy_ready and not has_signal_engine:
                continue

            # Status from state.json or artifacts
            status_val = "unknown"
            state_file = _load_json_file(d / "state.json")
            if state_file:
                status_val = str(state_file.get("status") or "unknown").lower()
            elif (d / "artifacts" / "equity.csv").exists():
                status_val = "success"
            elif (d / "review_report.json").exists():
                status_val = "success"

            # Parse created_at from run_id (YYYYMMDD_HHMMSS or run_YYYYMMDD_HHMMSS)
            created_at = "Unknown"
            if run_id.startswith("run_"):
                parts = run_id.split('_')
                if len(parts) >= 3:
                    d_str, t_str = parts[1], parts[2]
                    if len(d_str) == 8 and len(t_str) == 6:
                        created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"
            elif "_" in run_id:
                parts = run_id.split('_')
                if len(parts) >= 2:
                    d_str, t_str = parts[0], parts[1]
                    if len(d_str) == 8 and len(t_str) == 6:
                        created_at = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:8]} {t_str[:2]}:{t_str[2:4]}:{t_str[4:6]}"

            if created_at == "Unknown":
                mtime = datetime.fromtimestamp(d.stat().st_mtime)
                created_at = mtime.strftime("%Y-%m-%d %H:%M:%S")

            prompt = None
            # Explicit Monitor/API rename; kept separate from immutable prompt.
            stored_title: Optional[str] = None
            session_id: Optional[str] = None
            req_file = d / "req.json"
            planner_file = d / "planner_output.json"
            if req_file.exists():
                try:
                    req_data = json.loads(req_file.read_text(encoding="utf-8"))
                    prompt = req_data.get("prompt")
                    raw_title = req_data.get("title")
                    if isinstance(raw_title, str) and raw_title.strip():
                        stored_title = raw_title.strip()
                    # Session-linked runs store session_id in req.json context.
                    raw_sid = (req_data.get("context") or {}).get("session_id")
                    if isinstance(raw_sid, str) and raw_sid.strip():
                        session_id = raw_sid.strip()
                except (json.JSONDecodeError, OSError):
                    pass

            if not prompt and planner_file.exists():
                try:
                    planner_data = json.loads(planner_file.read_text(encoding="utf-8"))
                    prompt = planner_data.get("user_goal") or planner_data.get("goal")
                except (json.JSONDecodeError, OSError):
                    pass

            if not prompt:
                prompt_file = d / "user_prompt.txt"
                if prompt_file.exists():
                    prompt = prompt_file.read_text(encoding="utf-8").strip()

            total_return = None
            excess_return = None
            max_drawdown = None
            sharpe = None
            metrics_file = d / "artifacts" / "metrics.csv"
            if metrics_file.exists():
                try:
                    with open(metrics_file, 'r', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            total_return = float(row.get('total_return', 0) or 0)
                            sharpe = float(row.get('sharpe', 0) or 0)
                            if row.get('excess_return') not in (None, ""):
                                excess_return = float(row.get('excess_return') or 0)
                            if row.get('max_drawdown') not in (None, ""):
                                max_drawdown = float(row.get('max_drawdown') or 0)
                            break
                except (OSError, ValueError):
                    pass

            prompt_text = prompt or "Manual Analysis"
            # Sidebar rename → session.title; Monitor rename → req.json title.
            # Prefer session so chat rename stays the live label when both exist.
            display_title = _session_title(session_id) if session_id else None
            if not display_title:
                display_title = stored_title or prompt_text

            run_context = load_run_context(d)
            results.append(RunInfo(
                run_id=run_id,
                status=status_val,
                created_at=created_at,
                prompt=prompt_text,
                title=display_title,
                total_return=total_return,
                excess_return=excess_return,
                max_drawdown=max_drawdown,
                sharpe=sharpe,
                codes=run_context.get("codes") or [],
                start_date=run_context.get("start_date"),
                end_date=run_context.get("end_date"),
                has_signal_engine=has_signal_engine,
            ))

        return results

    @app.patch("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def update_run(run_id: str, req: UpdateRunRequest):
        """Rename a backtest run's display title (Monitor strategy-source list).

        Persists ``title`` on ``req.json`` without changing the immutable prompt.
        When the run is linked to a chat session, the session title is synced so
        the sidebar label stays aligned.
        """
        _host_validate_path_param(run_id, "run_id")
        if req.title is None:
            raise HTTPException(status_code=400, detail="title is required")
        cleaned_title = req.title.strip()
        if not cleaned_title:
            raise HTTPException(status_code=400, detail="title must not be empty")

        runs_root = _host_RUNS_DIR().resolve()
        run_dir = (runs_root / run_id).resolve()
        try:
            run_dir.relative_to(runs_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid run_id") from exc
        if not run_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        req_path = run_dir / "req.json"
        req_data: Dict[str, Any] = {}
        if req_path.exists():
            try:
                loaded = json.loads(req_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    req_data = loaded
            except (json.JSONDecodeError, OSError) as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to read req.json for run {run_id}: {exc}",
                ) from exc

        req_data["title"] = cleaned_title
        try:
            req_path.write_text(
                json.dumps(req_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.exception("failed to write title for run %s", run_id)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to update run {run_id}: {exc}",
            ) from exc

        # Keep the linked chat label in sync when this run came from a session.
        session_id: Optional[str] = None
        raw_sid = (req_data.get("context") or {}).get("session_id")
        if isinstance(raw_sid, str) and raw_sid.strip():
            session_id = raw_sid.strip()
        if session_id:
            from src.session.store import SessionStore

            try:
                session_store = SessionStore(base_dir=_host_SESSIONS_DIR())
                session = session_store.get_session(session_id)
                if session is not None:
                    session.title = cleaned_title
                    session.updated_at = datetime.now(timezone.utc).isoformat()
                    session_store.update_session(session)
            except Exception:
                # Run rename already persisted; session sync is best-effort.
                logger.exception(
                    "failed to sync session title for run %s session %s",
                    run_id,
                    session_id,
                )

        return {"status": "updated", "run_id": run_id, "title": cleaned_title}

    @app.delete("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def delete_run(run_id: str):
        """Delete a backtest run directory and its monitor board/history.

        Used by the Monitor strategy-source picker so operators can prune old
        runs when the dropdown becomes hard to scan.
        """
        _host_validate_path_param(run_id, "run_id")
        runs_root = _host_RUNS_DIR().resolve()
        run_dir = (runs_root / run_id).resolve()
        # Refuse anything that escapes RUNS_DIR (symlink / .. tricks).
        try:
            run_dir.relative_to(runs_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid run_id") from exc

        if not run_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found",
            )

        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            logger.exception("failed to delete run directory %s", run_dir)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete run {run_id}: {exc}",
            ) from exc

        # Drop the per-strategy monitor board + scan history for this run_id.
        from src.monitor.store import delete_board_and_history_for_run

        cleanup = delete_board_and_history_for_run(run_id)
        return {
            "status": "deleted",
            "run_id": run_id,
            "monitor_cleanup": cleanup,
        }
