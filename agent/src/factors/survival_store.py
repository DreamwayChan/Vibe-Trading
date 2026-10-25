"""Persist latest bench survival status per alpha id.

Factor Zoo definitions stay static in ``zoo/*.py``. Bench outcomes (alive /
reversed / dead) are written here after each full-zoo run so the catalogue
can filter by 「是否存活」 without re-running a bench.

Storage: ``~/.vibe-trading/alpha_survival.json`` (atomic replace).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

_STORE_FILENAME = "alpha_survival.json"
_LOCK = threading.Lock()

# Wire categories from ``bench_runner.categorise``.
_VALID_CATEGORIES = frozenset({"alive", "reversed", "dead"})
# Catalogue filter values exposed by ``GET /alpha/list?alive_status=``.
_VALID_ALIVE_STATUSES = frozenset({"alive", "invalid", "unknown"})


def survival_path() -> Path:
    """Return the on-disk path for the survival catalogue."""
    path = Path.home() / ".vibe-trading" / _STORE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def category_to_alive_status(category: str | None) -> str:
    """Map a bench category to the catalogue binary/ternary filter value.

    ``alive`` stays alive; ``dead`` and ``reversed`` both count as invalid
    (失效) for the 「是否存活」 filter. Missing / unknown categories → unknown.
    """
    if category == "alive":
        return "alive"
    if category in ("dead", "reversed"):
        return "invalid"
    return "unknown"


def record_bench_survival(
    rows: Iterable[Mapping[str, Any]],
    *,
    zoo: str,
    universe: str,
    period: str,
) -> int:
    """Upsert survival rows from a completed full-zoo bench.

    Args:
        rows: Per-alpha bench rows that already carry ``_category`` (or
            ``category``) plus IC stats.
        zoo / universe / period: Bench context stored alongside each alpha.

    Returns:
        Number of alpha records written.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates: dict[str, dict[str, Any]] = {}
    for row in rows:
        alpha_id = str(row.get("id") or "").strip()
        if not alpha_id:
            continue
        category = str(row.get("_category") or row.get("category") or "").strip()
        if category not in _VALID_CATEGORIES:
            continue
        updates[alpha_id] = {
            "id": alpha_id,
            "category": category,
            "alive_status": category_to_alive_status(category),
            "zoo": zoo,
            "universe": universe,
            "period": period,
            "ic_mean": row.get("ic_mean"),
            "ir": row.get("ir"),
            "updated_at": now,
        }

    if not updates:
        return 0

    with _LOCK:
        payload = _read_unlocked()
        alphas = payload.setdefault("alphas", {})
        if not isinstance(alphas, dict):
            alphas = {}
            payload["alphas"] = alphas
        alphas.update(updates)
        payload["updated_at"] = now
        _write_unlocked(payload)

    logger.info(
        "survival store: upserted %d alphas (zoo=%s universe=%s period=%s)",
        len(updates),
        zoo,
        universe,
        period,
    )
    return len(updates)


def get_status(alpha_id: str) -> dict[str, Any] | None:
    """Return the stored survival record for one alpha, or ``None``."""
    with _LOCK:
        payload = _read_unlocked()
    alphas = payload.get("alphas") or {}
    record = alphas.get(alpha_id)
    return dict(record) if isinstance(record, dict) else None


def get_status_map(alpha_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Return ``alpha_id → record`` for the requested ids (or the full store)."""
    with _LOCK:
        payload = _read_unlocked()
    alphas = payload.get("alphas") or {}
    if not isinstance(alphas, dict):
        return {}
    if alpha_ids is None:
        return {
            str(aid): dict(rec)
            for aid, rec in alphas.items()
            if isinstance(rec, dict)
        }
    out: dict[str, dict[str, Any]] = {}
    for aid in alpha_ids:
        rec = alphas.get(aid)
        if isinstance(rec, dict):
            out[str(aid)] = dict(rec)
    return out


def alive_status_of(record: Mapping[str, Any] | None) -> str:
    """Resolve ``alive_status`` for a stored record (unknown when missing)."""
    if not record:
        return "unknown"
    stored = record.get("alive_status")
    if stored in _VALID_ALIVE_STATUSES and stored != "unknown":
        return str(stored)
    return category_to_alive_status(record.get("category"))  # type: ignore[arg-type]


def _read_unlocked() -> dict[str, Any]:
    path = survival_path()
    if not path.is_file():
        return {"updated_at": None, "alphas": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("survival store read failed (%s); starting empty", exc)
        return {"updated_at": None, "alphas": {}}
    if not isinstance(raw, dict):
        return {"updated_at": None, "alphas": {}}
    raw.setdefault("alphas", {})
    return raw


def _write_unlocked(payload: dict[str, Any]) -> None:
    """Atomic JSON replace so a crash mid-write cannot truncate the catalogue."""
    path = survival_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".alpha_survival.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
