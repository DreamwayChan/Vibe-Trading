"""Shared monkeypatch compatibility layer for extracted API modules.

Tests monkeypatch ``api_server._API_KEY`` (and similar).  Route modules
resolve dependencies via ``sys.modules["api_server"]``, so the patched
value lives on the *host* module, not on the extracted module.

The host is also aliased when the server is launched as a script
(``python api_server.py``): Python names that module ``__main__``, so every
route module would fail to find its host and ``register_runs_routes`` would
abort the import — a launch path that worked only through the packaged
``vibe-trading serve`` entry point. ``_adopt_main_module()`` runs at import of
this module, which happens before the first route is registered.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def _adopt_main_module() -> None:
    """Register ``__main__`` as ``api_server`` when it *is* this server."""
    if "api_server" in sys.modules:
        return
    main = sys.modules.get("__main__")
    if main is None:
        return
    if os.path.basename(str(getattr(main, "__file__", ""))) == "api_server.py":
        sys.modules["api_server"] = main


_adopt_main_module()


def host_attr(name: str, fallback: Any) -> Any:
    """Read a compatibility attribute from ``api_server`` when present."""
    host = sys.modules.get("api_server")
    if host is not None and hasattr(host, name):
        return getattr(host, name)
    return fallback


def set_host_attr(name: str, value: Any) -> None:
    """Write a compatibility attribute onto ``api_server`` when present."""
    host = sys.modules.get("api_server")
    if host is not None:
        setattr(host, name, value)
