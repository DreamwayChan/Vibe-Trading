"""Environment-variable configuration for the table module.

These are tool-local knobs, so they stay out of the pydantic
:class:`EnvConfig` schema — the same pattern the existing tools use for their
own intervals (e.g. ``research_reports_tool._THS_MIN_INTERVAL_ENV``). Reading is
lazy: a value changed after import still takes effect on the next call, which is
why they go through :func:`src.config.accessor.read_env` rather than the cached
config singleton.
"""

from __future__ import annotations

from pathlib import Path

from src.config.accessor import read_env

DEFAULT_MAX_PAGES = 15
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_CACHE_MAX_ENTRIES = 200
DEFAULT_CACHE_MAX_BYTES = 500 * 1024 * 1024

_ENGINE_ENV = "VIBE_TRADING_TABLE_ENGINE"
_MAX_PAGES_ENV = "VIBE_TRADING_TABLE_MAX_PAGES"
_TIMEOUT_ENV = "VIBE_TRADING_TABLE_TIMEOUT_S"
_CACHE_ENV = "VIBE_TRADING_TABLE_CACHE"
_CACHE_DIR_ENV = "VIBE_TRADING_TABLE_CACHE_DIR"
_SUBPROCESS_ENV = "VIBE_TRADING_TABLE_SUBPROCESS"

_VALID_ENGINES = ("auto", "docling", "heuristic", "off")


def _env_str(name: str, default: str) -> str:
    value = read_env(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = read_env(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = read_env(name)
    if not raw:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = read_env(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def table_engine_name() -> str:
    """Requested engine: ``auto`` / ``docling`` / ``heuristic`` / ``off``."""
    value = _env_str(_ENGINE_ENV, "auto").lower()
    return value if value in _VALID_ENGINES else "auto"


def max_pages() -> int:
    """Maximum pages converted per call (protects against 300-page annual reports)."""
    value = _env_int(_MAX_PAGES_ENV, DEFAULT_MAX_PAGES)
    return value if value > 0 else DEFAULT_MAX_PAGES


def timeout_s() -> float:
    """Cooperative wall-clock budget for one extraction call."""
    value = _env_float(_TIMEOUT_ENV, DEFAULT_TIMEOUT_S)
    return value if value > 0 else DEFAULT_TIMEOUT_S


def cache_enabled() -> bool:
    return _env_bool(_CACHE_ENV, True)


def cache_dir() -> Path:
    """On-disk cache root, next to the other ``~/.vibe-trading`` state."""
    raw = read_env(_CACHE_DIR_ENV)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / ".vibe-trading" / "cache" / "tables"


def subprocess_enabled() -> bool:
    """Run the (heavy) engine in a child process. Reserved for P1/docling."""
    return _env_bool(_SUBPROCESS_ENV, False)


def cache_max_entries() -> int:
    return DEFAULT_CACHE_MAX_ENTRIES


def cache_max_bytes() -> int:
    return DEFAULT_CACHE_MAX_BYTES
