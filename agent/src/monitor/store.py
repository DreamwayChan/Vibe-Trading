"""Persist per-strategy monitor boards and append-only daily holdings history.

Layout under ``~/.vibe-trading/monitor/``::

    index.json                 # { "active_board_id": "..." }
    boards/<board_id>.json     # one MonitorConfig per strategy source
    default.json               # legacy single-board file (migrated once)
    history/<run_id>/...       # daily snapshots (already keyed by strategy)
    forecasts/<run_id>/...     # user-saved forecast compare reports
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.monitor.models import DayOps, MonitorConfig, MonitorDaySnapshot, StrategyRef

logger = logging.getLogger(__name__)

_MONITOR_FILENAME = "default.json"
_INDEX_FILENAME = "index.json"
_BOARDS_DIRNAME = "boards"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Explicit forecast saves (not daily scan snapshots): 20260819T024600Z[_xxxx]
_FORECAST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
# Cap so repeated saves cannot fill the home directory unbounded.
MAX_FORECAST_SAVES = 100
# Board ids are derived from strategy pointers (run_… / shadow_… / draft).
_SAFE_BOARD_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,100}$")
_DRAFT_BOARD_ID = "draft"


def monitor_dir() -> Path:
    """Return ``~/.vibe-trading/monitor/``, creating it when missing."""
    path = Path.home() / ".vibe-trading" / "monitor"
    path.mkdir(parents=True, exist_ok=True)
    return path


def boards_dir() -> Path:
    """Directory holding one JSON file per strategy-bound board."""
    path = monitor_dir() / _BOARDS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def index_path() -> Path:
    return monitor_dir() / _INDEX_FILENAME


def monitor_path() -> Path:
    """Legacy single-board path (kept for migration / older tests)."""
    return monitor_dir() / _MONITOR_FILENAME


def board_path(board_id: str) -> Path:
    return boards_dir() / f"{_safe_board_id(board_id)}.json"


def history_dir(run_id: str) -> Path:
    """Return ``~/.vibe-trading/monitor/history/<run_id>/``."""
    safe = _safe_run_id(run_id)
    path = monitor_dir() / "history" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot_path(run_id: str, as_of: str) -> Path:
    """Path for one day's snapshot (same-day scans overwrite this file)."""
    if not _DATE_RE.match(as_of):
        raise ValueError(f"invalid as_of date: {as_of!r}")
    return history_dir(run_id) / f"{as_of}.json"


def board_id_for_strategy(strategy: StrategyRef | None) -> str:
    """Stable filesystem key for a strategy pointer.

    Each distinct run (or shadow profile) gets its own board so watchlists and
    latest scan results do not overwrite each other when the UI switches source.
    """
    if strategy is None:
        return _DRAFT_BOARD_ID
    if strategy.type == "shadow":
        shadow_id = (strategy.shadow_id or "").strip()
        if shadow_id and is_safe_history_key(shadow_id):
            return f"shadow_{shadow_id}"
        return _DRAFT_BOARD_ID
    run_id = (strategy.run_id or "").strip()
    if run_id and is_safe_history_key(run_id):
        return f"run_{run_id}"
    return _DRAFT_BOARD_ID


def load_monitor() -> MonitorConfig:
    """Load the active strategy board (migrating legacy ``default.json`` if needed)."""
    _migrate_legacy_default_if_needed()
    board_id = get_active_board_id()
    return load_board(board_id)


def save_monitor(config: MonitorConfig) -> Path:
    """Persist ``config`` under its strategy board and mark that board active."""
    board_id = board_id_for_strategy(config.strategy)
    path = save_board(board_id, config)
    set_active_board_id(board_id)
    return path


def load_board(board_id: str) -> MonitorConfig:
    """Load one board file, or an empty config when absent/corrupt."""
    try:
        path = board_path(board_id)
    except ValueError:
        return MonitorConfig()
    if not path.is_file():
        return MonitorConfig()
    return _read_config(path)


def save_board(board_id: str, config: MonitorConfig) -> Path:
    """Atomically write one strategy board."""
    path = board_path(board_id)
    return _atomic_write_json(path, config.to_dict())


def load_board_for_strategy(strategy: StrategyRef) -> MonitorConfig:
    """Load the board bound to ``strategy``, or a fresh empty board for it.

    Does not copy symbols/results from any other strategy — switching sources
    must show that source's own stored content (or blanks until first save/scan).
    """
    board_id = board_id_for_strategy(strategy)
    config = load_board(board_id)
    # Always reflect the requested pointer (draft boards may have been empty).
    config.strategy = StrategyRef(
        type=strategy.type,
        run_id=strategy.run_id,
        shadow_id=strategy.shadow_id,
    )
    return config


def get_active_board_id() -> str:
    """Return the active board id from ``index.json``, defaulting to draft."""
    path = index_path()
    if not path.is_file():
        return _DRAFT_BOARD_ID
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("monitor index unreadable (%s); using draft board", exc)
        return _DRAFT_BOARD_ID
    if not isinstance(raw, dict):
        return _DRAFT_BOARD_ID
    board_id = str(raw.get("active_board_id") or "").strip()
    if board_id and _SAFE_BOARD_ID.match(board_id):
        return board_id
    return _DRAFT_BOARD_ID


def set_active_board_id(board_id: str) -> None:
    """Remember which strategy board the UI / scheduled scan should open."""
    safe = _safe_board_id(board_id)
    _atomic_write_json(index_path(), {"active_board_id": safe})


def list_board_ids() -> list[str]:
    """Return sorted board ids that currently have a JSON file."""
    root = boards_dir()
    ids = [path.stem for path in root.glob("*.json") if _SAFE_BOARD_ID.match(path.stem)]
    ids.sort()
    return ids


def delete_board_and_history_for_run(run_id: str) -> dict[str, bool]:
    """Remove the monitor board + daily history bound to a deleted backtest run.

    Called after the run directory itself is removed so the strategy-source
    picker does not leave orphan boards / history that clutter maintenance.
    Safe to call when neither board nor history exists.
    """
    text = str(run_id or "").strip()
    if not is_safe_history_key(text):
        return {"board": False, "history": False, "cleared_active": False}

    removed_board = False
    board_id = f"run_{text}"
    try:
        path = board_path(board_id)
    except ValueError:
        path = None
    if path is not None and path.is_file():
        try:
            path.unlink()
            removed_board = True
        except OSError as exc:
            logger.warning("failed to delete monitor board %s: %s", path, exc)

    cleared_active = False
    if get_active_board_id() == board_id:
        # Fall back to the empty draft board so GET /monitor stays valid.
        set_active_board_id(_DRAFT_BOARD_ID)
        cleared_active = True

    removed_history = False
    # Do not call history_dir() here — that helper creates the directory.
    hist_root = monitor_dir() / "history" / text
    if hist_root.is_dir():
        try:
            shutil.rmtree(hist_root)
            removed_history = True
        except OSError as exc:
            logger.warning("failed to delete monitor history %s: %s", hist_root, exc)

    return {
        "board": removed_board,
        "history": removed_history,
        "cleared_active": cleared_active,
    }


def clear_strategy_history(history_key: str) -> bool:
    """Delete all daily snapshots under ``history/<history_key>/``.

    Used by monitor reset: wipe prior enter/exit baselines so the next scan
    starts as day one. Does not delete the strategy board (symbols/schedule).
    Returns True only when snapshot files (or a non-empty tree) were removed.
    """
    text = str(history_key or "").strip()
    if not is_safe_history_key(text):
        raise ValueError(f"invalid history key: {history_key!r}")
    # Do not call history_dir() — that helper creates the directory.
    hist_root = monitor_dir() / "history" / text
    if not hist_root.is_dir():
        return False
    # list_snapshot_dates may recreate an empty folder; treat that as a no-op.
    try:
        has_entries = any(hist_root.iterdir())
    except OSError:
        has_entries = True
    if not has_entries:
        try:
            hist_root.rmdir()
        except OSError:
            pass
        return False
    try:
        shutil.rmtree(hist_root)
    except OSError as exc:
        logger.warning("failed to clear monitor history %s: %s", hist_root, exc)
        raise
    return True


def clear_board_scan_cache(config: MonitorConfig) -> MonitorConfig:
    """Drop last-scan holdings/suggestions so the board matches an empty history."""
    config.suggestions = []
    config.holdings = []
    config.ops = DayOps()
    config.last_scan_at = None
    return config


def save_day_snapshot(snapshot: MonitorDaySnapshot) -> Path:
    """Write/overwrite the daily snapshot for ``snapshot.run_id`` + ``as_of``."""
    path = snapshot_path(snapshot.run_id, snapshot.as_of)
    return _atomic_write_json(path, snapshot.to_dict())


def load_day_snapshot(run_id: str, as_of: str) -> MonitorDaySnapshot | None:
    """Load one day's snapshot, or None when missing/corrupt."""
    try:
        path = snapshot_path(run_id, as_of)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("monitor snapshot unreadable (%s): %s", path, exc)
        return None
    return MonitorDaySnapshot.from_dict(raw)


def load_previous_snapshot(run_id: str, as_of: str) -> MonitorDaySnapshot | None:
    """Return the newest holdings snapshot strictly before ``as_of``.

    Same-day rescans must always baseline against this prior-day book — today's
    intraday scans overwrite the ``as_of`` file for display only and must not
    become the reference for later scans on the same calendar date.
    """
    dates = list_snapshot_dates(run_id)
    prior = [d for d in dates if d < as_of]
    if not prior:
        return None
    return load_day_snapshot(run_id, prior[-1])


def list_snapshot_dates(run_id: str) -> list[str]:
    """Sorted ascending list of ``YYYY-MM-DD`` dates with snapshots."""
    try:
        root = history_dir(run_id)
    except ValueError:
        return []
    dates: list[str] = []
    for path in root.glob("*.json"):
        stem = path.stem
        if _DATE_RE.match(stem):
            dates.append(stem)
    dates.sort()
    return dates


def list_snapshot_summaries(run_id: str, *, limit: int = 30) -> list[dict]:
    """Newest-first summaries for history list UI."""
    dates = list_snapshot_dates(run_id)
    if limit > 0:
        dates = dates[-limit:]
    summaries: list[dict] = []
    for as_of in reversed(dates):
        snap = load_day_snapshot(run_id, as_of)
        if snap is None:
            continue
        summaries.append(
            {
                "as_of": snap.as_of,
                "scanned_at": snap.scanned_at,
                "run_id": snap.run_id,
                "provisional": snap.provisional,
                "holding_count": len(snap.holdings),
                "entered_count": len(snap.ops.entered),
                "exited_count": len(snap.ops.exited),
                "held_count": len(snap.ops.held),
                "universe_size": snap.universe_size,
                "fetch_error_count": snap.fetch_error_count,
                "live_quote_ok": snap.live_quote_ok,
                "live_quote_total": snap.live_quote_total,
                "live_quote_coverage": snap.live_quote_coverage,
            }
        )
    return summaries


def is_safe_history_key(run_id: str) -> bool:
    """Return True when ``run_id`` is safe to use as a history directory name."""
    text = str(run_id or "").strip()
    return bool(text and _SAFE_RUN_ID.match(text))


def _migrate_legacy_default_if_needed() -> None:
    """One-time: copy ``default.json`` into ``boards/`` when no boards exist yet."""
    legacy = monitor_path()
    if not legacy.is_file():
        return
    # Prefer the multi-board layout once any board file exists.
    if any(boards_dir().glob("*.json")):
        return
    config = _read_config(legacy)
    board_id = board_id_for_strategy(config.strategy)
    try:
        save_board(board_id, config)
        set_active_board_id(board_id)
        logger.info("migrated legacy monitor default.json → boards/%s.json", board_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to migrate legacy monitor board: %s", exc)


def _read_config(path: Path) -> MonitorConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("monitor config unreadable (%s); using empty default", exc)
        return MonitorConfig()
    if not isinstance(raw, dict):
        return MonitorConfig()
    return MonitorConfig.from_dict(raw)


def _safe_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not is_safe_history_key(text):
        raise ValueError(f"invalid run_id for history path: {run_id!r}")
    return text


def _safe_board_id(board_id: str) -> str:
    text = str(board_id or "").strip()
    if not text or not _SAFE_BOARD_ID.match(text):
        raise ValueError(f"invalid board_id: {board_id!r}")
    return text


def _atomic_write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".monitor_",
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


def forecasts_dir(run_id: str) -> Path:
    """Return ``~/.vibe-trading/monitor/forecasts/<run_id>/``."""
    path = monitor_dir() / "forecasts" / _safe_run_id(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def forecast_snapshot_path(run_id: str, snapshot_id: str) -> Path:
    if not is_safe_forecast_id(snapshot_id):
        raise ValueError(f"invalid forecast snapshot id: {snapshot_id!r}")
    return forecasts_dir(run_id) / f"{snapshot_id}.json"


def is_safe_forecast_id(snapshot_id: str) -> bool:
    text = str(snapshot_id or "").strip()
    return bool(text and _FORECAST_ID_RE.match(text))


def _new_forecast_id(run_id: str) -> str:
    """UTC timestamp id; suffix when two saves land in the same second."""
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not forecast_snapshot_path(run_id, base).is_file():
        return base
    for suffix in range(16):
        candidate = f"{base}_{suffix:x}"
        if not forecast_snapshot_path(run_id, candidate).is_file():
            return candidate
    return f"{base}_{os.urandom(2).hex()}"


def save_forecast_snapshot(run_id: str, report: dict) -> dict:
    """Persist one forecast report. Returns the stored envelope (id + payload)."""
    if not isinstance(report, dict):
        raise ValueError("forecast report must be an object")
    saved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_id = _new_forecast_id(run_id)
    payload = {
        "id": snapshot_id,
        "saved_at": saved_at,
        "run_id": _safe_run_id(run_id),
        "as_of": report.get("as_of"),
        "horizon": int(report.get("horizon") or 10),
        "holding_count": int(report.get("holding_count") or 0),
        "evaluated_count": int(report.get("evaluated_count") or 0),
        "capped": bool(report.get("capped")),
        "bias": str(report.get("bias") or "neutral"),
        "items": list(report.get("items") or []),
    }
    path = forecast_snapshot_path(run_id, snapshot_id)
    _atomic_write_json(path, payload)
    _prune_forecast_saves(run_id)
    return payload


def load_forecast_snapshot(run_id: str, snapshot_id: str) -> dict | None:
    try:
        path = forecast_snapshot_path(run_id, snapshot_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("forecast snapshot unreadable (%s): %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def list_forecast_summaries(run_id: str, *, limit: int = 50) -> list[dict]:
    """Newest-first saved forecast envelopes (without the heavy ``items`` payload)."""
    try:
        root = forecasts_dir(run_id)
    except ValueError:
        return []
    rows: list[dict] = []
    for path in root.glob("*.json"):
        snap = load_forecast_snapshot(run_id, path.stem)
        if snap is None:
            continue
        rows.append(
            {
                "id": str(snap.get("id") or path.stem),
                "saved_at": str(snap.get("saved_at") or ""),
                "run_id": str(snap.get("run_id") or run_id),
                "as_of": snap.get("as_of"),
                "bias": str(snap.get("bias") or "neutral"),
                "holding_count": int(snap.get("holding_count") or 0),
                "evaluated_count": int(snap.get("evaluated_count") or 0),
            }
        )
    rows.sort(key=lambda row: (row.get("saved_at") or "", row.get("id") or ""), reverse=True)
    if limit > 0:
        rows = rows[:limit]
    return rows


def _prune_forecast_saves(run_id: str) -> None:
    """Drop oldest files when a strategy exceeds ``MAX_FORECAST_SAVES``."""
    try:
        root = forecasts_dir(run_id)
    except ValueError:
        return
    files = sorted(root.glob("*.json"), key=lambda p: p.stem)
    overflow = len(files) - MAX_FORECAST_SAVES
    if overflow <= 0:
        return
    for path in files[:overflow]:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("failed to prune forecast save %s: %s", path, exc)
