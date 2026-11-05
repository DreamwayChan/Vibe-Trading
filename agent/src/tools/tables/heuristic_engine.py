"""Dependency-free table engine built on pypdfium2 text geometry.

Chosen as the default because research-report PDFs usually carry a real text
layer, and the geometry primitives (:mod:`src.tools.tables.geometry`) recover
rows and columns without downloading a model. Its known blind spot is merged
cells and slanted / multi-row headers — the structural engine (docling, P1) is
what fixes those.

Quality contract: this engine reports ``confidence=None``. It genuinely cannot
estimate its own accuracy, and inventing a number would make the model trust a
bad column split.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from src.tools.tables.geometry import detect_grid, looks_like_table, read_text_rects
from src.tools.tables.models import RawBatch
from src.tools.tables.normalize import cells_to_raw


class HeuristicTableEngine:
    """Recover tables from text-rectangle geometry."""

    name = "heuristic"
    version = "1"
    install_hint = "pypdfium2 ships with the base install (pip install pypdfium2)"

    def is_available(self) -> bool:
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            return False
        return True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(
        self,
        path: Path,
        pages: list[int],
        *,
        max_tables: int = 20,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> RawBatch:
        import pypdfium2 as pdfium

        batch = RawBatch()
        total = len(pages)
        doc = pdfium.PdfDocument(str(path))
        try:
            page_count = len(doc)
            for idx, page_no in enumerate(pages, start=1):
                if deadline is not None and time.monotonic() > deadline:
                    batch.partial = True
                    break
                if not 1 <= page_no <= page_count:
                    continue
                page = doc[page_no - 1]
                try:
                    rects = read_text_rects(page)
                finally:
                    page.close()

                grid = detect_grid(rects)
                if looks_like_table(grid):
                    batch.tables.append(
                        cells_to_raw(
                            grid.cells,
                            page=page_no,
                            header_rows=grid.header_rows,
                            caption=grid.caption,
                            bbox=grid.bbox,
                        )
                    )
                    if len(batch.tables) >= max_tables:
                        batch.partial = page_no != pages[-1]
                        batch.pages_read.append(page_no)
                        if progress:
                            progress(idx, total)
                        break

                batch.pages_read.append(page_no)
                if progress:
                    progress(idx, total)
        finally:
            doc.close()
        return batch
