"""Locked theme vs live rank map.

Rank order is the observation map (which board looks hottest). The lock is
the order: keep one healthy theme until it fails, or a challenger leads for
enough days with a real score gap. ``max_themes=1`` is this single lock.
Index-bear days still tick the sesame counter; they just skip opening a book.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.sector_pick.models import SectorScore

# Calendar days the map leader must stay #1 before it may steal a healthy lock.
CHALLENGER_CONFIRM_DAYS = 2
# live_score units: timing bonus is 0 or 1, ranks add ~0–1. 0.25 is "clearly ahead".
CHALLENGER_SCORE_MARGIN = 0.25


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class ThemeLock:
    """Persisted execution theme. Survives rank-1 flips while the book is healthy."""

    locked_key: str | None = None
    challenger_key: str | None = None
    challenger_days: int = 0
    # Last as_of that incremented (or started) the challenger streak.
    challenger_as_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "locked_key": self.locked_key,
            "challenger_key": self.challenger_key,
            "challenger_days": int(self.challenger_days),
            "challenger_as_of": self.challenger_as_of,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> ThemeLock:
        if not isinstance(payload, dict):
            return cls()
        try:
            days = int(payload.get("challenger_days") or 0)
        except (TypeError, ValueError):
            days = 0
        return cls(
            locked_key=_optional_str(payload.get("locked_key")),
            challenger_key=_optional_str(payload.get("challenger_key")),
            challenger_days=max(0, days),
            challenger_as_of=_optional_str(payload.get("challenger_as_of")),
        )

    def cleared_challenger(self) -> ThemeLock:
        """Keep the lock; drop a sesame that no longer leads."""
        return ThemeLock(locked_key=self.locked_key)


def score_lead(challenger: SectorScore, locked: SectorScore) -> float:
    """Positive when the map leader's live_score is above the locked theme."""
    return float(challenger.live_score or 0.0) - float(locked.live_score or 0.0)


def tick_challenger(
    lock: ThemeLock,
    *,
    map_row: SectorScore | None,
    locked_row: SectorScore,
    as_of: str,
) -> ThemeLock:
    """Count consecutive days the map leader is clearly ahead of a healthy lock.

    Same-day rescans must not increment: ``challenger_as_of == as_of`` freezes
    the streak so clicking Scan twice today is not two confirm days.
    """
    if map_row is None or map_row.key == locked_row.key:
        return lock.cleared_challenger()
    if score_lead(map_row, locked_row) < CHALLENGER_SCORE_MARGIN:
        return lock.cleared_challenger()

    if lock.challenger_as_of == as_of:
        if lock.challenger_key != map_row.key:
            # Map leader changed on a rescan of the same day — restart, still one day.
            return ThemeLock(
                locked_key=lock.locked_key,
                challenger_key=map_row.key,
                challenger_days=1,
                challenger_as_of=as_of,
            )
        return ThemeLock(
            locked_key=lock.locked_key,
            challenger_key=lock.challenger_key,
            challenger_days=lock.challenger_days,
            challenger_as_of=lock.challenger_as_of,
        )

    days = lock.challenger_days + 1 if lock.challenger_key == map_row.key else 1
    return ThemeLock(
        locked_key=lock.locked_key,
        challenger_key=map_row.key,
        challenger_days=days,
        challenger_as_of=as_of,
    )


def should_steal(
    lock: ThemeLock,
    *,
    map_row: SectorScore | None,
    locked_row: SectorScore | None,
) -> bool:
    """True when a sesame has earned the lock (days + score gap), lock still healthy."""
    if locked_row is None or map_row is None:
        return False
    if lock.challenger_days < CHALLENGER_CONFIRM_DAYS:
        return False
    if not lock.challenger_key or lock.challenger_key != map_row.key:
        return False
    return score_lead(map_row, locked_row) >= CHALLENGER_SCORE_MARGIN


def advance_lock_identity(
    lock: ThemeLock,
    *,
    map_row: SectorScore | None,
    locked_row: SectorScore | None,
    as_of: str,
) -> ThemeLock:
    """Tick the map-leader streak and steal the lock key when it has earned it.

    Does not probe strategy books. Index-bear days still rank the map; they
    must not freeze ``challenger_days`` or the UI stays on 「已领先 1/2 日」
    while rank-1 has already been the sesame for two scans. Book health is
    re-checked on the next day that is allowed to open a book.
    """
    if locked_row is None or not lock_row_is_eligible(locked_row):
        return lock
    next_lock = tick_challenger(lock, map_row=map_row, locked_row=locked_row, as_of=as_of)
    if not should_steal(next_lock, map_row=map_row, locked_row=locked_row):
        return next_lock
    # Steal is map-only here: leftover books are skipped when buying resumes.
    if map_row is None or not lock_row_is_eligible(map_row):
        return lock.cleared_challenger()
    return ThemeLock(locked_key=map_row.key)


def row_by_key(rows: list[SectorScore], key: str | None) -> SectorScore | None:
    if not key:
        return None
    return next((row for row in rows if row.key == key), None)


def lock_row_is_eligible(row: SectorScore | None) -> bool:
    """Disabled / failed / unscored locks cannot be held — treat as failure."""
    if row is None:
        return False
    if not row.enabled or row.error:
        return False
    return row.rank is not None
