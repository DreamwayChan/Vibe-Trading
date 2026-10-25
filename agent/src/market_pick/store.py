"""Persist daily market-pick snapshots. GET never re-fetches market data.

Layout::

    ~/.vibe-trading/market_pick/history/YYYY-MM-DD.json

Same-day refreshes overwrite that date's file so history stays one row per day.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from src.market_pick.models import MarketPickResult

logger = logging.getLogger(__name__)

_HISTORY_DIRNAME = "history"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AlreadyScannedToday(ValueError):
    """Today's snapshot exists; caller must pass force=True to rebuild it."""

    def __init__(self, result: MarketPickResult) -> None:
        super().__init__("today's market-pick snapshot already exists")
        self.result = result


def market_pick_dir() -> Path:
    path = Path.home() / ".vibe-trading" / "market_pick"
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_dir() -> Path:
    path = market_pick_dir() / _HISTORY_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot_path(as_of: str) -> Path:
    """Path for one day's screen (same-day scans overwrite this file)."""
    if not _DATE_RE.match(as_of):
        raise ValueError(f"invalid as_of date: {as_of!r}")
    return history_dir() / f"{as_of}.json"


def load_day_snapshot(as_of: str) -> MarketPickResult | None:
    try:
        path = snapshot_path(as_of)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("market-pick snapshot unreadable (%s): %s", path, exc)
        return None
    return MarketPickResult.from_dict(raw if isinstance(raw, dict) else None)


def save_day_snapshot(result: MarketPickResult) -> Path:
    """Write/overwrite the daily snapshot for ``result.as_of``."""
    return _atomic_write_json(snapshot_path(result.as_of), result.to_dict())


def load_latest_snapshot() -> MarketPickResult | None:
    """Newest calendar-day file, or None when the folder is empty."""
    dates = list_snapshot_dates()
    if not dates:
        return None
    return load_day_snapshot(dates[-1])


def require_fresh_scan(as_of: str, *, force: bool) -> MarketPickResult | None:
    """Return the existing snapshot when a same-day rescan is blocked."""
    existing = load_day_snapshot(as_of)
    if existing is not None and not force:
        raise AlreadyScannedToday(existing)
    return existing


def list_snapshot_dates() -> list[str]:
    """Sorted ascending list of ``YYYY-MM-DD`` dates with snapshots."""
    root = market_pick_dir() / _HISTORY_DIRNAME
    if not root.is_dir():
        return []
    dates = [path.stem for path in root.glob("*.json") if _DATE_RE.match(path.stem)]
    dates.sort()
    return dates


def list_snapshot_summaries(*, limit: int = 30) -> list[dict]:
    """Newest-first summaries for the history date pills."""
    dates = list_snapshot_dates()
    if limit > 0:
        dates = dates[-limit:]
    summaries: list[dict] = []
    for as_of in reversed(dates):
        snap = load_day_snapshot(as_of)
        if snap is None:
            continue
        summaries.append(
            {
                "as_of": snap.as_of,
                "scanned_at": snap.scanned_at,
                "recommended_bk": snap.recommended_bk,
                "recommended_name": _recommended_name(snap),
                "map_bk": snap.map_bk,
                "stance": snap.stance,
                "market_regime": snap.market_regime,
                "board_count": len(snap.boards),
                "unique_symbols": snap.unique_symbols,
                "ohlcv_ok": snap.ohlcv_ok,
                "fetch_error_count": snap.fetch_error_count,
            }
        )
    return summaries


def _recommended_name(result: MarketPickResult) -> str | None:
    """Board name for the day's winner, or None when nothing was recommended."""
    bk = result.recommended_bk
    if not bk:
        return None
    for row in result.boards:
        if row.bk == bk:
            return row.name
    return None


def _atomic_write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".market_pick_",
        suffix=".json.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path
