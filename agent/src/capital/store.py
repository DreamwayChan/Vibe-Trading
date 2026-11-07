"""Persistence for the capital-flow monitor.

Layout under ``~/.vibe-trading/capital/``::

    series.json            # last successfully built ETF-OI report
    cache/shares/*.json    # per-day exchange share snapshots (see sources.py)
    cache/nav/*.json       # per-fund NAV history
    cache/index/*.json     # benchmark index closes
    cache/mcap/*.json      # CSI 300 market-cap anchor
    cache/weights/*.json   # CSI 300 constituent weights

``series.json`` exists so the page can still render the last known reading when
every upstream source is unreachable — a stale chart with a timestamp beats an
error page for a monitoring tool.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.capital.models import EtfOiReport

logger = logging.getLogger(__name__)

_SERIES_FILENAME = "series.json"


def capital_dir() -> Path:
    """``~/.vibe-trading/capital`` (created on demand)."""
    path = Path.home() / ".vibe-trading" / "capital"
    path.mkdir(parents=True, exist_ok=True)
    return path


def series_path() -> Path:
    return capital_dir() / _SERIES_FILENAME


def save_report(report: EtfOiReport) -> None:
    """Persist the report atomically (best effort)."""
    path = series_path()
    try:
        payload = {"saved_at": datetime.now(timezone.utc).isoformat(), "report": report.to_dict()}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:  # pragma: no cover - environmental
        logger.warning("failed to persist capital series: %s", exc)


def load_report() -> EtfOiReport | None:
    """Load the last saved report, or ``None`` when unavailable."""
    path = series_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("failed to read capital series: %s", exc)
        return None
    raw = payload.get("report") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        report = EtfOiReport.from_dict(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("malformed capital series: %s", exc)
        return None
    report.cached = True
    return report
