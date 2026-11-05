"""Child-process entry point for table extraction (``VIBE_TRADING_TABLE_SUBPROCESS=1``).

Two problems this solves, both real when the API server serves table
extraction:

1. **No hard timeout otherwise.** docling's own ``document_timeout`` is
   cooperative (checked between pages) and only exists in recent minors. A
   table-heavy 60-page PDF can block a request thread for minutes. A child
   process can be killed.
2. **Resident memory.** torch plus the layout/table models keep ~1.5 GB alive
   for as long as the process runs. A child releases it on exit.

Protocol (deliberately dumb so a crash is diagnosable):

* argv: ``python -m src.tools.tables._worker <engine-name>``
* stdin: one JSON object ``{"path": str, "pages": [int], "max_tables": int}``
* stdout: one JSON object — ``{"ok": true, "tables": [...], "pages_read": [...],
  "partial": bool}`` or ``{"ok": false, "error": str}``
* exit code 0 on success, 1 on failure.

Anything the engine logs goes to stderr, so stdout stays a single parseable
line. The child resolves the engine through :func:`resolve_engine`, never
:func:`get_table_engine`, otherwise the inherited environment variable would
spawn a child from the child.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from src.tools.tables.engine import TableEngineDisabled, TableEngineUnavailable, resolve_engine


def main(argv: list[str] | None = None) -> int:
    # stdout is the wire; logs must not pollute it.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(json.dumps({"ok": False, "error": "usage: _worker <engine>"}))
        return 2

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"bad stdin payload: {exc}"}))
        return 2

    path = Path(str(payload.get("path", "")))
    pages = [int(p) for p in payload.get("pages") or []]
    max_tables = int(payload.get("max_tables") or 20)

    try:
        engine = resolve_engine(args[0])
        batch = engine.extract(path, pages, max_tables=max_tables, deadline=None, progress=None)
    except (TableEngineDisabled, TableEngineUnavailable) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "tables": [t.to_dict() for t in batch.tables],
                "pages_read": batch.pages_read,
                "partial": batch.partial,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through a real subprocess
    raise SystemExit(main())
