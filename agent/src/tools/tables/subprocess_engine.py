"""Run a table engine in a child process (``VIBE_TRADING_TABLE_SUBPROCESS=1``).

The wrapper exists for two reasons a long-lived server cares about: the child can
be **killed** when the wall-clock budget expires (docling's own ``document_timeout``
is cooperative and missing from older minors), and its torch models are released
when the request ends instead of staying resident.

Design notes:

* The wrapper forwards ``name`` / ``version`` from the inner engine, so the tool's
  cache key and its ``quality`` field are identical with or without isolation —
  switching the flag never invalidates the cache or changes the envelope.
* A timeout is an **error**, never an empty result. The child buffers its output,
  so nothing can be salvaged from a killed process; reporting "no tables found"
  would be a lie that the model would then reason about.
* The child's stderr is surfaced (truncated) in the raised message: a torch
  import failure is otherwise invisible behind "worker exited with code 1".

**Known cost**: starting the child takes ~2.3 s even for the geometric engine,
because importing any ``src.tools.tables.*`` module runs ``src/tools/__init__.py``,
which eagerly discovers all 77 tools. That is noise next to docling (30-60 s per
page) but a 20x penalty on the heuristic engine, so isolation is off by default
and makes sense mainly for the server case it was designed for.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from src.tools.tables.engine import TableEngine
from src.tools.tables.models import RawBatch, RawTable

# agent/ — the package root that makes ``-m src.tools.tables._worker`` importable.
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]

_STDERR_TAIL = 600


class SubprocessTableEngine:
    """Delegates :meth:`TableEngine.extract` to a child process."""

    def __init__(self, inner: TableEngine) -> None:
        self._inner = inner
        self.name = inner.name
        self.version = inner.version
        self.install_hint = inner.install_hint

    def is_available(self) -> bool:
        return self._inner.is_available()

    def supports(self, path: Path) -> bool:
        return self._inner.supports(path)

    # -- internals ---------------------------------------------------------

    def command(self) -> list[str]:
        """The child command. Overridable so tests can substitute a stub."""
        return [sys.executable, "-m", "src.tools.tables._worker", self.name]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Belt and braces: the child resolves its engine with resolve_engine(),
        # but clear the flag so a future refactor cannot spawn a grandchild.
        env["VIBE_TRADING_TABLE_SUBPROCESS"] = "0"
        # Chinese cell text must survive the pipe on a cp936 Windows console.
        env["PYTHONIOENCODING"] = "utf-8"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_PACKAGE_ROOT}{os.pathsep}{existing}" if existing else str(_PACKAGE_ROOT)
        )
        return env

    def extract(
        self,
        path: Path,
        pages: list[int],
        *,
        max_tables: int = 20,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> RawBatch:
        timeout = 300.0
        if deadline is not None:
            timeout = max(1.0, deadline - time.monotonic())

        payload = json.dumps(
            {"path": str(path), "pages": pages, "max_tables": max_tables},
            ensure_ascii=False,
        )

        if progress:
            # One coarse event: the child reports nothing until it finishes.
            progress(0, len(pages))

        try:
            completed = subprocess.run(
                self.command(),
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._env(),
                cwd=str(_PACKAGE_ROOT),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"table extraction exceeded {timeout:.0f}s and was killed "
                f"(engine={self.name}); narrow the `pages` range or raise "
                "VIBE_TRADING_TABLE_TIMEOUT_S"
            ) from None

        stdout = (completed.stdout or "").strip()
        try:
            data = json.loads(stdout.splitlines()[-1]) if stdout else {}
        except (json.JSONDecodeError, IndexError):
            data = {}

        if not data.get("ok"):
            detail = data.get("error") or (completed.stderr or "").strip()[-_STDERR_TAIL:]
            raise RuntimeError(
                f"table worker failed (engine={self.name}, exit={completed.returncode}): "
                f"{detail or 'no output'}"
            )

        batch = RawBatch(partial=bool(data.get("partial", False)))
        batch.pages_read = [int(p) for p in data.get("pages_read") or []]
        for item in data.get("tables") or []:
            try:
                batch.tables.append(RawTable.from_dict(item))
            except (TypeError, ValueError, KeyError):
                continue
        return batch
