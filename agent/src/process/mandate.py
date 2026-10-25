"""Process Mandate — constitution for the A-share research loop.

This is not the broker live-trading mandate in ``src.live.mandate``.
That file gates real orders. This one answers: may today's theme book
add risk, and how large, before any overlay is allowed to speak.

Numeric knobs are safe to tune. ``veto_order`` is not — later gates may
only tighten, never reopen a closed door.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProcessMandate:
    """Hard constraints the compose waterfall cannot override.

    Attributes:
        book_nav_cap: Max fraction of NAV this theme book may occupy.
        max_name_weight: Max single-name weight after clipping.
        max_themes: How many theme books may be recommended at once.
            Sector-pick enforces this as a single ThemeLock (rank-1 is the map).
        half_watch_mult: Size multiplier when F0 stance is half_watch.
        allow_short: Retail A-share accounts cannot short.
        provisional_is_decision: If False, an intraday proxy bar cannot
            authorize new risk (scan may still warn).
        analog_on_daily_scan: If False, daily scans skip kNN analog.
    """

    book_nav_cap: float = 0.40
    max_name_weight: float = 0.50
    max_themes: int = 1
    half_watch_mult: float = 0.50
    allow_short: bool = False
    provisional_is_decision: bool = False
    analog_on_daily_scan: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable hash so historical snapshots know which constitution applied."""
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# Tunable defaults from the rollout plan. Tests pin this object.
DEFAULT_MANDATE = ProcessMandate()
