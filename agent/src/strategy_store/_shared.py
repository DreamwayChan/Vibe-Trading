"""Shared store singleton for SDM tools.

All SDM tools (sdm_register, sdm_status, sdm_decay_scan) import
``get_store()`` from this module to share a single ``SqliteStrategyStore``
instance.  The SQLite backend provides WAL-mode persistence with FK
constraints and schema migrations via ``PRAGMA user_version``.
"""

from __future__ import annotations

from src.strategy_store.sqlite_store import SqliteStrategyStore

_store: SqliteStrategyStore | None = None


def get_store() -> SqliteStrategyStore:
    """Return the process-wide strategy store singleton.

    Reopens when *this* function opened the store and the configured database
    path has since changed (env override or a different home in tests). A
    store assigned onto the singleton by a caller is left in place.
    """
    global _store
    from src.strategy_store.sqlite_store import _default_db_path

    path = _default_db_path()
    managed = _store is not None and getattr(_store, "_opened_by_get_store", False)
    if _store is not None and not managed:
        return _store
    if _store is None or _store.db_path != path:
        if _store is not None:
            try:
                _store._conn.close()
            except Exception:  # noqa: BLE001 — replacing the singleton
                pass
        _store = SqliteStrategyStore(path)
        _store._opened_by_get_store = True  # type: ignore[attr-defined]
    return _store
