"""Resolve the full strategy symbol universe for monitor holdings scans."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_CODE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def load_run_universe(run_dir: Path, *, fallback: list[str] | None = None) -> list[str]:
    """Return strategy codes for a run, preferring config.json then list files.

    Cross-sectional engines need the full panel to rank correctly. The monitor
    watchlist (≤20) is only for alert suggestions and must not replace this.

    Args:
        run_dir: ``agent/runs/<run_id>`` directory.
        fallback: Used when the run has no discoverable codes (usually the board
            watchlist).

    Returns:
        Deduplicated upper-cased symbols in first-seen order.
    """
    codes = _codes_from_config(run_dir / "config.json")
    if not codes:
        codes = _codes_from_list_files(run_dir)
    if not codes and fallback:
        logger.info(
            "monitor universe: run %s has no codes; falling back to watchlist (%d)",
            run_dir.name,
            len(fallback),
        )
        codes = list(fallback)
    return _normalize_codes(codes)


def _codes_from_config(config_path: Path) -> list[str]:
    if not config_path.is_file():
        return []
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    return _normalize_codes(raw.get("codes") or [])


def _codes_from_list_files(run_dir: Path) -> list[str]:
    """Pick up common stock-list text dumps written beside the run."""
    candidates = sorted(
        [
            *run_dir.glob("all_*.txt"),
            *run_dir.glob("*_stocks.txt"),
            *run_dir.glob("universe*.txt"),
        ]
    )
    for path in candidates:
        codes = _codes_from_text_file(path)
        if codes:
            logger.info(
                "monitor universe: loaded %d codes from %s",
                len(codes),
                path.name,
            )
            return codes
    return []


def _codes_from_text_file(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    tokens: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Support comma / whitespace separated lines.
        for part in re.split(r"[,，\s]+", line):
            part = part.strip()
            if part:
                tokens.append(part)
    return _normalize_codes(tokens)


def _normalize_codes(raw: list[str] | object) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        if not _CODE_TOKEN.match(symbol):
            continue
        seen.add(symbol)
        out.append(symbol)
    return out
