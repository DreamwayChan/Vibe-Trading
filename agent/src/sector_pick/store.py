"""Persist sector-pick enabled-set, last scan, and daily snapshots.

Layout::

    ~/.vibe-trading/sector_pick/state.json
    ~/.vibe-trading/sector_pick/history/YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from src.monitor.holdings import build_holdings_and_ops, previous_weights_from_holdings
from src.monitor.trend_regime import sort_holdings_by_trend
from src.sector_pick.catalog import default_enabled_keys, validate_enabled_keys
from src.sector_pick.models import SectorPickResult, SectorPickState

logger = logging.getLogger(__name__)

_STATE_FILENAME = "state.json"
_HISTORY_DIRNAME = "history"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sector_pick_dir() -> Path:
    """Return ``~/.vibe-trading/sector_pick/``, creating it when missing."""
    path = Path.home() / ".vibe-trading" / "sector_pick"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return sector_pick_dir() / _STATE_FILENAME


def history_dir() -> Path:
    path = sector_pick_dir() / _HISTORY_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot_path(as_of: str) -> Path:
    """Path for one day's overlay snapshot (same-day scans overwrite this file)."""
    if not _DATE_RE.match(as_of):
        raise ValueError(f"invalid as_of date: {as_of!r}")
    return history_dir() / f"{as_of}.json"


def load_state() -> SectorPickState:
    """Load persisted state; unknown keys are dropped, empty enabled → all."""
    path = state_path()
    if not path.is_file():
        return SectorPickState(enabled_keys=default_enabled_keys())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sector-pick state unreadable (%s); using defaults", exc)
        return SectorPickState(enabled_keys=default_enabled_keys())
    if not isinstance(raw, dict):
        return SectorPickState(enabled_keys=default_enabled_keys())
    state = SectorPickState.from_dict(raw)
    # Missing field → opt in all; explicit [] means the user unchecked every row.
    if "enabled_keys" not in raw:
        state.enabled_keys = default_enabled_keys()
    else:
        known = set(default_enabled_keys())
        kept = [key for key in state.enabled_keys if key in known]
        state.enabled_keys = kept
    return state


def save_state(state: SectorPickState) -> Path:
    """Atomically replace ``state.json``."""
    try:
        state.enabled_keys = validate_enabled_keys(state.enabled_keys)
    except ValueError:
        state.enabled_keys = default_enabled_keys()
    return _atomic_write_json(state_path(), state.to_dict())


def commit_scan_result(result: SectorPickResult) -> SectorPickResult:
    """Rebase enter/exit vs the prior calendar day, then overwrite today's file.

    Same-day rescans replace ``history/<as_of>.json`` for display, but ops still
    diff against the last snapshot *before* today — matching the monitor board.
    """
    prior = load_previous_snapshot(result.as_of)
    current_weights = previous_weights_from_holdings(result.holdings)
    previous_weights = (
        previous_weights_from_holdings(prior.holdings) if prior is not None else None
    )
    meta = {
        row.symbol: {
            "price": row.price,
            "data_as_of": row.data_as_of,
            "provisional": row.provisional,
            "trend": row.trend,
        }
        for row in result.holdings
    }
    holdings, ops = build_holdings_and_ops(
        current_weights,
        previous_weights,
        meta_by_symbol=meta,
    )
    # Rebase rewrites rows for enter/trim; restore dual-track order if trend is present.
    result.holdings = sort_holdings_by_trend(holdings)
    result.ops = ops
    save_day_snapshot(result)
    return result


def save_day_snapshot(result: SectorPickResult) -> Path:
    """Write/overwrite the daily snapshot for ``result.as_of``."""
    return _atomic_write_json(snapshot_path(result.as_of), result.to_dict())


def load_day_snapshot(as_of: str) -> SectorPickResult | None:
    """Load one day's overlay snapshot, or None when missing/corrupt."""
    try:
        path = snapshot_path(as_of)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sector-pick snapshot unreadable (%s): %s", path, exc)
        return None
    return SectorPickResult.from_dict(raw if isinstance(raw, dict) else None)


def load_previous_snapshot(as_of: str) -> SectorPickResult | None:
    """Newest snapshot strictly before ``as_of`` (never a same-day file)."""
    dates = list_snapshot_dates()
    prior = [day for day in dates if day < as_of]
    if not prior:
        return None
    return load_day_snapshot(prior[-1])


def list_snapshot_dates() -> list[str]:
    """Sorted ascending list of ``YYYY-MM-DD`` dates with snapshots."""
    root = history_dir()
    dates: list[str] = []
    for path in root.glob("*.json"):
        if _DATE_RE.match(path.stem):
            dates.append(path.stem)
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
                "recommended_key": snap.recommended_key,
                "holdings_key": snap.holdings_key,
                "stance": snap.stance,
                "provisional": snap.provisional,
                "holding_count": len(snap.holdings),
                "entered_count": len(snap.ops.entered),
                "exited_count": len(snap.ops.exited),
                "held_count": len(snap.ops.held),
            }
        )
    return summaries


def list_history_series(*, limit: int = 30) -> dict:
    """Chronological per-day ranks and live metrics for the history chart.

    Oldest date first so the x-axis reads left-to-right. Same-day overwrite
    already collapsed each date to one snapshot.
    """
    dates = list_snapshot_dates()
    if limit > 0:
        dates = dates[-limit:]
    days: list[dict] = []
    for as_of in dates:
        snap = load_day_snapshot(as_of)
        if snap is None:
            continue
        sectors: list[dict] = []
        for row in snap.sectors:
            if row.rank is None and row.ret_20d is None:
                continue
            sectors.append(
                {
                    "key": row.key,
                    "name": row.name,
                    "rank": row.rank,
                    "ret_20d": row.ret_20d,
                    "confirm_pass_rate": row.confirm_pass_rate,
                    "timing_mult": row.timing_mult,
                    "live_score": row.live_score,
                }
            )
        days.append(
            {
                "as_of": snap.as_of,
                "recommended_key": snap.recommended_key,
                "stance": snap.stance,
                "sectors": sectors,
            }
        )
    return {"days": days}


def _atomic_write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".sector_pick_",
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
