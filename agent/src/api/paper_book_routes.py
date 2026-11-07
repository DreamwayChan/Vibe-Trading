"""HTTP day-step for the paper book.

Mounted by ``agent/api_server.py`` via ``register_paper_book_routes(app)``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.monitor.models import StrategyRef
from src.monitor.store import (
    board_id_for_strategy,
    load_board_for_strategy,
    load_monitor,
    save_board,
)
from src.paper_book.book import resolve_run_dir, step_day
from src.paper_book.soak import calendar_kind, plan_window, step_range
from src.paper_book.store import (
    PaperBookError,
    load_report,
    load_state,
    load_window,
    paper_book_root,
    require_book_id,
)

logger = logging.getLogger(__name__)


class StepPaperBookRequest(BaseModel):
    """Settle one session from the monitor board bound to ``run_id``."""

    as_of: str = Field(..., description="Session date YYYY-MM-DD")
    run_id: Optional[str] = Field(None, description="Backtest run id. Default: active monitor board")
    book_id: Optional[str] = Field(None, description="Paper book id. Default: the run id")


class SoakPaperBookRequest(BaseModel):
    """Settle consecutive exchange sessions and return the window report.

    Pass ``start`` and ``end``, or ``end`` plus ``days`` (default 20). Weekends
    and exchange holidays are not sessions.
    """

    start: Optional[str] = Field(None, description="First calendar day YYYY-MM-DD")
    end: Optional[str] = Field(None, description="Last calendar day YYYY-MM-DD")
    days: Optional[int] = Field(
        None,
        description="Session count ending on `end`. Default 20 when `start` is omitted",
    )
    run_id: Optional[str] = Field(None, description="Backtest run id. Default: active monitor board")
    book_id: Optional[str] = Field(None, description="Paper book id. Default: the run id")


def register_paper_book_routes(
    app: FastAPI,
    require_auth: Any | None = None,
) -> None:
    """Register paper-book step, soak, and read routes."""
    import sys as _sys

    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if require_auth is None:
        require_auth = host.require_auth

    @app.post("/paper-book/step", dependencies=[Depends(require_auth)])
    async def post_paper_book_step(body: StepPaperBookRequest) -> dict:
        run_id = (body.run_id or "").strip()
        if run_id:
            config = load_board_for_strategy(StrategyRef(type="run_dir", run_id=run_id))
        else:
            config = load_monitor()
            run_id = (config.strategy.run_id or "").strip()
        try:
            report = step_day(
                as_of=body.as_of,
                config=config,
                book_id=(body.book_id or "").strip() or None,
                run_dir=resolve_run_dir(run_id),
            )
        except PaperBookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("paper book step failed")
            raise HTTPException(status_code=500, detail=f"paper book step failed: {exc}") from exc
        if report.downgraded:
            save_board(board_id_for_strategy(config.strategy), config)
        return report.to_dict()

    @app.post("/paper-book/soak", dependencies=[Depends(require_auth)])
    async def post_paper_book_soak(body: SoakPaperBookRequest) -> dict:
        run_id = (body.run_id or "").strip()
        if run_id:
            config = load_board_for_strategy(StrategyRef(type="run_dir", run_id=run_id))
        else:
            config = load_monitor()
            run_id = (config.strategy.run_id or "").strip()
        try:
            start, end, sessions = plan_window(start=body.start, end=body.end, days=body.days)
            report = step_range(
                start=start,
                end=end,
                sessions=sessions,
                calendar=calendar_kind() if sessions is not None else None,
                config=config,
                book_id=(body.book_id or "").strip() or None,
                run_dir=resolve_run_dir(run_id),
            )
        except PaperBookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("paper book soak failed")
            raise HTTPException(status_code=500, detail=f"paper book soak failed: {exc}") from exc
        if report.downgraded:
            save_board(board_id_for_strategy(config.strategy), config)
        return report.to_dict()

    @app.get("/paper-book/{book_id}", dependencies=[Depends(require_auth)])
    async def get_paper_book(
        book_id: str,
        as_of: Optional[str] = None,
        window: bool = False,
    ) -> dict:
        try:
            safe = require_book_id(book_id)
        except PaperBookError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state = load_state(paper_book_root(), safe)
        if state is None:
            raise HTTPException(status_code=404, detail=f"no paper book named {safe}")
        day = (as_of or state.settled_through or "").strip()
        report = None
        if day:
            try:
                report = load_report(paper_book_root(), safe, day)
            except PaperBookError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = {"state": state.to_dict(), "report": report}
        if window:
            payload["window"] = load_window(paper_book_root(), safe)
        return payload
