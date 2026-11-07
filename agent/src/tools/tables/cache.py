"""On-disk cache for extracted tables.

A research report is read repeatedly in practice (extract numbers → draft the
write-up → audit the numbers), and extraction costs tens of seconds while a
cache hit costs milliseconds. Cache failures are never fatal: every helper here
swallows its own errors and degrades to "no cache".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Sequence

from src.tools.tables import config
from src.tools.tables.models import NORMALIZE_VERSION, TableRecord

logger = logging.getLogger(__name__)

_SUFFIX = ".json"


def file_digest(path: Path) -> str:
    """Stream a SHA-256 of the file contents."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_key(
    path: Path,
    engine_name: str,
    engine_version: str | None,
    pages: Sequence[int],
) -> str | None:
    """Build a cache key, or ``None`` when the key cannot be computed.

    The key covers file content (not mtime), engine identity, the exact page
    set, and :data:`NORMALIZE_VERSION` — so a rules change or an engine upgrade
    invalidates old entries instead of silently mixing formats.
    """
    try:
        digest = file_digest(path)
    except OSError as exc:
        logger.debug("table cache: cannot hash %s: %s", path, exc)
        return None
    payload = "|".join(
        (
            digest,
            engine_name,
            engine_version or "",
            ",".join(str(p) for p in pages),
            NORMALIZE_VERSION,
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _entry_path(key: str) -> Path:
    return config.cache_dir() / f"{key}{_SUFFIX}"


def load(key: str | None) -> list[TableRecord] | None:
    """Read cached records for ``key``; ``None`` on miss or any error."""
    if key is None or not config.cache_enabled():
        return None
    path = _entry_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("table cache: unreadable entry %s: %s", path, exc)
        return None

    try:
        records = [TableRecord.from_dict(item) for item in payload.get("tables", [])]
    except Exception as exc:
        logger.debug("table cache: malformed entry %s: %s", path, exc)
        return None

    # Refresh mtime so LRU eviction treats this entry as recently used.
    try:
        os.utime(path, None)
    except OSError:
        pass
    return records


def save(key: str | None, records: Sequence[TableRecord]) -> None:
    """Persist records for ``key`` (atomic replace), then enforce the size caps."""
    if key is None or not config.cache_enabled():
        return
    directory = config.cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("table cache: cannot create %s: %s", directory, exc)
        return

    payload = {
        "saved_at": int(time.time()),
        "normalize_version": NORMALIZE_VERSION,
        "tables": [r.to_dict() for r in records],
    }
    target = _entry_path(key)
    tmp = target.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.debug("table cache: cannot write %s: %s", target, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return

    _evict()


def _evict() -> None:
    """Drop least-recently-used entries until both caps hold."""
    directory = config.cache_dir()
    try:
        entries = sorted(
            (p for p in directory.iterdir() if p.suffix == _SUFFIX),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return

    total = 0
    sizes: dict[Path, int] = {}
    for entry in entries:
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        sizes[entry] = size
        total += size

    max_entries = config.cache_max_entries()
    max_bytes = config.cache_max_bytes()

    for entry in entries:  # oldest first
        size = sizes.get(entry, 0)
        if len(sizes) <= max_entries and total <= max_bytes:
            break
        try:
            entry.unlink()
        except OSError:
            continue
        sizes.pop(entry, None)
        total -= size


def clear() -> int:
    """Delete every cached entry. Returns the number of files removed."""
    directory = config.cache_dir()
    removed = 0
    try:
        for entry in directory.iterdir():
            if entry.suffix in (_SUFFIX, ".tmp"):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return removed
