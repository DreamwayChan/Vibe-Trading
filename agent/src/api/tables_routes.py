"""Research-report table extraction HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_tables_routes(app)``.

The handlers are deliberately thin: they resolve the uploaded file path and
call the exact same :func:`src.tools.tables.service.extract_tables` the agent's
``extract_tables`` tool calls, so the Tables page and the agent can never
disagree about what a PDF contains. Everything else (engine selection, page
capping, continuation stitching, normalisation, caching) stays in the tool.

Only PDF is supported here — the same restriction as the tool — because the
docx/xlsx readers already return their tables.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from src.api.helpers import AGENT_DIR
from src.tools.tables import config as table_config
from src.tools.tables.engine import (
    TableEngineDisabled,
    TableEngineUnavailable,
    available_engines,
    resolve_engine,
)
from src.tools.tables.service import extract_tables

logger = logging.getLogger(__name__)

_QUALITY_BY_ENGINE = {"heuristic": "geometric", "docling": "structural"}


class ExtractTablesRequest(BaseModel):
    """Body of ``POST /tables/extract``."""

    file_path: str = Field(description="Absolute path, or a path relative to the agent directory (e.g. 'uploads/<hex>.pdf').")
    pages: str = Field(default="", description="1-based page selection: '1-10', '5', '1,3,5-8'. Empty = all pages.")
    max_tables: int = Field(default=20, ge=1, le=100)
    include_csv: bool = True


def _resolve_path(raw: str) -> Path:
    """Turn the caller's path into an absolute one under the agent directory.

    Uploaded files come back from ``POST /upload`` as ``uploads/<hex>.pdf``;
    resolving those against the agent root keeps the browser from needing to
    know anything about the server's filesystem layout. The final security
    check still happens inside the tool (``safe_document_path``).
    """
    value = (raw or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="file_path is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = AGENT_DIR / path
    return path


def _engine_status() -> dict[str, Any]:
    """Describe the engine the next extraction would use (diagnostics banner)."""
    requested = table_config.table_engine_name()
    available = available_engines()
    try:
        engine = resolve_engine()
    except (TableEngineDisabled, TableEngineUnavailable) as exc:
        return {
            "status": "unavailable",
            "requested": requested,
            "engine": None,
            "available": available,
            "error": str(exc),
            "max_pages": table_config.max_pages(),
            "cache": table_config.cache_enabled(),
        }
    return {
        "status": "ok",
        "requested": requested,
        "engine": engine.name,
        "version": engine.version,
        "quality": _QUALITY_BY_ENGINE.get(engine.name, "unknown"),
        "available": available,
        "max_pages": table_config.max_pages(),
        "timeout_s": table_config.timeout_s(),
        "cache": table_config.cache_enabled(),
    }


def register_tables_routes(app: FastAPI, require_auth: Any | None = None) -> None:
    """Register ``GET /tables/engine`` and ``POST /tables/extract``."""
    import sys as _sys

    if require_auth is None:
        host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        require_auth = host.require_auth

    @app.get("/tables/engine", dependencies=[Depends(require_auth)])
    async def get_tables_engine() -> dict[str, Any]:
        return await run_in_threadpool(_engine_status)

    @app.post("/tables/extract", dependencies=[Depends(require_auth)])
    async def post_tables_extract(body: ExtractTablesRequest) -> dict[str, Any]:
        path = _resolve_path(body.file_path)
        raw = await run_in_threadpool(
            extract_tables,
            str(path),
            body.pages,
            max_tables=body.max_tables,
            include_csv=body.include_csv,
        )
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
            logger.exception("extract_tables returned a non-JSON payload")
            raise HTTPException(
                status_code=500, detail="table extraction returned a malformed payload"
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=500, detail="table extraction returned an unexpected payload")
        if payload.get("status") == "error":
            raise HTTPException(
                status_code=400,
                detail=str(payload.get("error") or "table extraction failed"),
            )
        return payload
