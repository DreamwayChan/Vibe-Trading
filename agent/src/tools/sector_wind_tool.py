"""Research-only tool: A-share sector wind detection and wind-gated factor cards.

Long-only, T+1. Does not place orders. Wraps ``src.sector_pick.wind_scan``.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool

_VALID_ACTIONS = ("wind", "cards")


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


class SectorWindTool(BaseTool):
    name = "sector_wind"
    description = (
        "A-share sector 'wind' (market tailwind) research, long-only and T+1. "
        "action='wind': per catalog sector, the wind state (dormant/launch/main/"
        "divergence/fade), score, and whether the score predicted T+1-executable "
        "excess return vs CSI 300. action='cards': for sectors whose wind is "
        "actionable and validated, screen library factors in-sector (out-of-sample, "
        "shrunk IC, limit-up-open excluded) and return factor cards with applies/"
        "retire conditions. Research only: every row has tradable false "
        "(uncausal_scan) and cannot enter the tradable pool. No orders. "
        'Example: {"action": "cards", "keys": ["robot"]}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_VALID_ACTIONS)},
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Catalog sector keys (e.g. 'robot'). Omit for all.",
            },
            "horizon": {
                "type": "integer",
                "description": "Holding horizon in trading days (1-20). Default 5.",
                "default": 5,
            },
        },
        "required": ["action"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action")
        if action not in _VALID_ACTIONS:
            return _error(f"action must be one of {list(_VALID_ACTIONS)}")
        try:
            horizon = int(kwargs.get("horizon") or 5)
        except (TypeError, ValueError):
            return _error("horizon must be an integer")
        if not 1 <= horizon <= 20:
            return _error("horizon must be between 1 and 20")
        keys = kwargs.get("keys") or None
        if keys is not None:
            from src.sector_pick.catalog import validate_enabled_keys

            try:
                keys = validate_enabled_keys(list(keys))
            except ValueError as exc:
                return _error(str(exc))

        try:
            from src.sector_pick import wind_scan

            if action == "wind":
                data: Any = wind_scan.run_wind_scan(keys, horizon=horizon)
            else:
                data = wind_scan.run_wind_cards(keys, horizon=horizon)
        except Exception as exc:  # noqa: BLE001 — tool must return an envelope
            return _error(f"sector_wind failed: {exc}")
        return json.dumps({"ok": True, "action": action, "data": data}, ensure_ascii=False, default=str)
