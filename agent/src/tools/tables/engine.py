"""Pluggable table-extraction engine interface and factory.

Mirrors :mod:`src.tools.ocr.engine` on purpose: a :class:`Protocol`, a built-in
registry keyed by name, lazy loading so a heavy engine never enters the import
path, and an entry-point group (``vibe_trading.table_engines``) so a third
party can ship an engine as a pip package.

Selection comes from ``VIBE_TRADING_TABLE_ENGINE``:

===========  ==========================================================
``auto``     docling when importable, otherwise heuristic (default)
``docling``  require docling; raise with an install hint when missing
``heuristic`` force the dependency-free geometry engine
``off``      tool is excluded from the registry
===========  ==========================================================
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

from src.tools.tables import config
from src.tools.tables.models import RawBatch

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "vibe_trading.table_engines"


class TableEngineUnavailable(RuntimeError):
    """The requested engine cannot be used in this environment."""


class TableEngineDisabled(RuntimeError):
    """``VIBE_TRADING_TABLE_ENGINE=off`` — table extraction is switched off."""


@runtime_checkable
class TableEngine(Protocol):
    """Contract every table engine implements."""

    name: str
    version: str
    install_hint: str

    def is_available(self) -> bool:
        """True when this engine's dependencies are satisfied."""
        ...

    def supports(self, path: Path) -> bool:
        """True when this engine can read ``path``."""
        ...

    def extract(
        self,
        path: Path,
        pages: list[int],
        *,
        max_tables: int = 20,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> RawBatch:
        """Extract tables from the given 1-based pages.

        Stops early and sets ``RawBatch.partial`` when ``deadline`` (a
        ``time.monotonic()`` value) passes or ``max_tables`` is reached.
        """
        ...


def _load_heuristic() -> Optional[TableEngine]:
    from src.tools.tables.heuristic_engine import HeuristicTableEngine

    return HeuristicTableEngine()


def _load_docling() -> Optional[TableEngine]:
    """Return the docling engine, or ``None`` when it is not installed.

    The import is deferred so the module does not exist in the base install
    until P1 lands; when it does, no change here is needed.
    """
    try:
        from src.tools.tables.docling_engine import DoclingTableEngine  # type: ignore[import-not-found]
    except ImportError:
        return None
    return DoclingTableEngine()


# Registry of built-in loaders. Adding an engine means adding one entry.
_BUILTIN_LOADERS: dict[str, Callable[[], Optional[TableEngine]]] = {
    "heuristic": _load_heuristic,
    "docling": _load_docling,
}


def register_builtin(name: str, loader: Callable[[], Optional[TableEngine]]) -> None:
    """Register an engine loader (used by extensions and tests)."""
    _BUILTIN_LOADERS[name] = loader


def resolve_engine(name: str | None = None) -> TableEngine:
    """Resolve an engine *without* subprocess isolation.

    Used by the worker process: :func:`get_table_engine` would see the inherited
    ``VIBE_TRADING_TABLE_SUBPROCESS=1`` and spawn a child from the child.
    """
    requested = (name or config.table_engine_name()).strip().lower()

    if requested == "off":
        raise TableEngineDisabled("table extraction is disabled (VIBE_TRADING_TABLE_ENGINE=off)")

    if requested != "auto":
        loader = _BUILTIN_LOADERS.get(requested)
        if loader is None:
            known = ", ".join(sorted(_BUILTIN_LOADERS))
            raise TableEngineUnavailable(f"unknown table engine {requested!r}; known engines: {known}")
        engine = loader()
        if engine is None or not engine.is_available():
            hint = (
                engine.install_hint
                if engine is not None
                else "install the matching optional dependency"
            )
            raise TableEngineUnavailable(f"table engine {requested!r} is unavailable: {hint}")
        return engine

    # auto: prefer the structural engine, fall back to geometry.
    for candidate in ("docling", "heuristic"):
        loader = _BUILTIN_LOADERS.get(candidate)
        if loader is None:
            continue
        try:
            engine = loader()
        except Exception as exc:  # a broken optional dep must not break the tool
            logger.debug("Table engine %s failed to load: %s", candidate, exc)
            continue
        if engine is not None and engine.is_available():
            return engine

    raise TableEngineUnavailable(
        "no table engine available: pypdfium2 is required for the heuristic engine "
        "(pip install pypdfium2)"
    )


def get_table_engine(name: str | None = None) -> TableEngine:
    """Resolve the engine to use.

    Args:
        name: Explicit engine name, or ``None`` to read the env config.

    Returns:
        A ready-to-use engine instance, wrapped in a subprocess when
        ``VIBE_TRADING_TABLE_SUBPROCESS`` is enabled. The wrapper keeps the
        inner engine's ``name``/``version``, so cache keys and the ``quality``
        field are unaffected.

    Raises:
        TableEngineDisabled: when the requested engine is ``off``.
        TableEngineUnavailable: when an explicitly requested engine is missing.
    """
    engine = resolve_engine(name)
    if not config.subprocess_enabled():
        return engine

    from src.tools.tables.subprocess_engine import SubprocessTableEngine

    return SubprocessTableEngine(engine)


def available_engines() -> list[str]:
    """Names of engines usable in this environment (diagnostics / tests)."""
    names: list[str] = []
    for name, loader in _BUILTIN_LOADERS.items():
        try:
            engine = loader()
        except Exception:
            continue
        if engine is not None and engine.is_available():
            names.append(name)
    return sorted(names)
