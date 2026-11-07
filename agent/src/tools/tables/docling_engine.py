"""Structural table engine backed by docling (optional, P1).

Why this engine exists: the geometric engine (:mod:`heuristic_engine`) reads
text rectangles, so it cannot recover **merged cells** or **slanted / multi-row
headers** — it sees the rendered pixels, not the structure. docling runs a
layout + TableFormer model and returns an explicit grid with
``row_span`` / ``col_span``, which is exactly what broker report tables (which
merge "2024A" over two sub-columns) need.

Deliberate constraints:

* **Optional and lazy.** docling pulls in torch plus 0.5-1 GB of model weights.
  Nothing here is imported until ``VIBE_TRADING_TABLE_ENGINE`` selects it, and
  the base install keeps working via the geometric engine.
* **Serialised.** docling's pipeline holds non-thread-safe inference objects, so
  every conversion takes a module-level lock (:data:`_CONVERT_LOCK`). The API
  server calls tools from a thread pool; without this, concurrent requests
  corrupt each other's state.
* **Only the grid conversion is complex, so only that is a pure function.**
  :func:`build_raw_table` takes duck-typed docling objects and can therefore be
  unit-tested with fakes on a machine without docling installed. Everything
  else here is thin plumbing around ``DocumentConverter``.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from src.tools.tables import config
from src.tools.tables.models import Cell, RawBatch, RawTable
from src.tools.tables.normalize import clean_cell

logger = logging.getLogger(__name__)

# docling model objects are not thread-safe; the pipeline must be serialised.
_CONVERT_LOCK = threading.Lock()

# Merge page selections separated by fewer than this many pages into one
# conversion window. A gap of one page costs less to convert than to spin up a
# second pipeline pass over the same document.
_RUN_GAP = 1


@lru_cache(maxsize=1)
def _distribution_version() -> str:
    """Installed docling version, or ``"unknown"`` when metadata is missing."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("docling")
    except Exception:  # PackageNotFoundError or a broken dist-info
        return "unknown"


def page_runs(pages: list[int]) -> list[tuple[int, int]]:
    """Group sorted 1-based pages into contiguous ``(first, last)`` windows.

    ``DocumentConverter.convert`` only accepts a contiguous ``page_range``, so a
    sparse selection such as ``[4, 7, 8]`` becomes ``[(4, 4), (7, 8)]``.
    Small gaps are merged to avoid paying the per-conversion overhead twice.
    """
    ordered = sorted({p for p in pages if p > 0})
    if not ordered:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page - prev <= 1 + _RUN_GAP:
            prev = page
            continue
        runs.append((start, prev))
        start = prev = page
    runs.append((start, prev))
    return runs


def _span_pair(cell: Any) -> tuple[int, int, int, int]:
    """Return ``(row_start, row_end, col_start, col_end)`` for a docling cell."""
    r0 = int(getattr(cell, "start_row_offset_idx", 0) or 0)
    c0 = int(getattr(cell, "start_col_offset_idx", 0) or 0)
    r1 = int(getattr(cell, "end_row_offset_idx", r0 + 1) or r0 + 1)
    c1 = int(getattr(cell, "end_col_offset_idx", c0 + 1) or c0 + 1)
    return r0, max(r1, r0 + 1), c0, max(c1, c0 + 1)


def _bbox_tuple(table: Any) -> tuple[float, float, float, float]:
    """Convert docling's bbox to our ``(left, bottom, right, top)`` convention.

    docling reports ``l/t/r/b`` with a ``coord_origin`` that is TOPLEFT or
    BOTTOMLEFT depending on backend, so the vertical pair is normalised by
    ``min/max`` instead of trusting the field order.
    """
    prov = getattr(table, "prov", None) or []
    box = getattr(prov[0], "bbox", None) if prov else None
    if box is None:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        left = float(box.l)
        right = float(box.r)
        a, b = float(box.t), float(box.b)
    except (AttributeError, TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (min(left, right), min(a, b), max(left, right), max(a, b))


def table_page(table: Any) -> int | None:
    """First provenance page of a table (1-based), or ``None``."""
    prov = getattr(table, "prov", None) or []
    if not prov:
        return None
    page = getattr(prov[0], "page_no", None)
    try:
        return int(page) if page is not None else None
    except (TypeError, ValueError):
        return None


def table_caption(table: Any, doc: Any = None) -> str:
    """Table caption text, tolerating docling's two ways of exposing it."""
    caption_text = getattr(table, "caption_text", None)
    if callable(caption_text):
        try:
            value = caption_text(doc) if doc is not None else caption_text()
            if value:
                return clean_cell(str(value))
        except Exception:  # signature drift between minors must not fail the table
            pass

    captions = getattr(table, "captions", None) or []
    for ref in captions:
        text = getattr(ref, "text", None)
        if text:
            return clean_cell(str(text))
        resolve = getattr(ref, "resolve", None)
        if callable(resolve) and doc is not None:
            try:
                resolved = resolve(doc)
                text = getattr(resolved, "text", "")
                if text:
                    return clean_cell(str(text))
            except Exception:
                continue
    return ""


def build_raw_table(table: Any, doc: Any = None, *, page: int | None = None) -> RawTable | None:
    """Convert one docling ``TableItem`` into an engine-agnostic :class:`RawTable`.

    Cell spans are materialised into a rectangular grid because
    :func:`normalize.normalize_table` works on plain string rows:

    * the anchor slot of a merged cell carries the text;
    * covered slots stay empty — this is what the "merged header" forward-fill in
      :func:`normalize.flatten_columns` expects;
    * **except** row headers spanning several rows (a group label such as
      "白酒" over three product rows), where the label is repeated so each body
      row keeps its identity in the CSV.

    Returns ``None`` when the table has no usable geometry.
    """
    data = getattr(table, "data", None)
    if data is None:
        return None

    try:
        n_rows = int(getattr(data, "num_rows", 0) or 0)
        n_cols = int(getattr(data, "num_cols", 0) or 0)
    except (TypeError, ValueError):
        return None
    if n_rows <= 0 or n_cols <= 0:
        return None

    grid: list[list[Cell]] = [[Cell() for _ in range(n_cols)] for _ in range(n_rows)]
    header_rows = 0

    for cell in getattr(data, "table_cells", None) or []:
        r0, r1, c0, c1 = _span_pair(cell)
        if r0 >= n_rows or c0 >= n_cols:
            # docling occasionally reports offsets outside its own declared grid.
            continue
        r1 = min(r1, n_rows)
        c1 = min(c1, n_cols)

        text = clean_cell(str(getattr(cell, "text", "") or ""))
        grid[r0][c0] = Cell(text=text, row_span=r1 - r0, col_span=c1 - c0)

        if bool(getattr(cell, "column_header", False)):
            header_rows = max(header_rows, r1)
        elif (
            text
            and bool(getattr(cell, "row_header", False))
            and r1 - r0 > 1
            and c1 - c0 == 1
        ):
            # Body-only rule: a group label over several rows. The top-left
            # corner cell of a multi-row header is also a row_header, but
            # copying it down would replace the child header names that
            # ``flatten_columns`` needs (``["业务","2024A",""]`` must stay).
            for row in range(r0 + 1, r1):
                grid[row][c0] = Cell(text=text)

    # A header must leave at least one body row, otherwise the table is just a
    # caption strip and normalisation rejects it anyway.
    header_rows = max(0, min(header_rows, n_rows - 1))
    if header_rows == 0 and n_rows > 1:
        # docling did not flag any column header: match the geometric engine's
        # convention of treating the first row as the header.
        header_rows = 1

    return RawTable(
        page=page if page is not None else (table_page(table) or 0),
        bbox=_bbox_tuple(table),
        cells=grid,
        header_rows=header_rows,
        caption=table_caption(table, doc),
        # docling exposes no per-table accuracy score. Reporting None is honest;
        # inventing a number would make the model over-trust a bad grid.
        confidence=None,
    )


class DoclingTableEngine:
    """Structural table extraction via docling's TableFormer model."""

    name = "docling"
    install_hint = (
        "pip install 'docling>=2.28,<3'  "
        "(pulls in torch and downloads ~0.5-1 GB of layout/table models on first run)"
    )

    def __init__(self) -> None:
        self._converter: Any = None
        self._page_range_supported = True

    @property
    def version(self) -> str:
        return _distribution_version()

    def is_available(self) -> bool:
        try:
            import docling.document_converter  # noqa: F401
        except Exception:
            return False
        return True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    # -- converter ---------------------------------------------------------

    def _build_converter(self, budget_s: float) -> Any:
        import inspect

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        # Table structure is the whole point; page/picture rasterisation is not.
        options.do_table_structure = True
        for field in ("generate_page_images", "generate_picture_images"):
            try:
                setattr(options, field, False)
            except Exception:  # field renamed between minors — keep the default
                logger.debug("docling: cannot disable %s", field)
        # Checked cooperatively between pages: on expiry docling keeps the pages
        # it already finished and reports PARTIAL_SUCCESS.
        if "document_timeout" in getattr(type(options), "model_fields", {}):
            try:
                options.document_timeout = max(1.0, budget_s)
            except Exception:
                pass
        try:
            from docling.datamodel.pipeline_options import TableStructureOptions

            options.table_structure_options = TableStructureOptions(do_cell_matching=True)
        except Exception:
            logger.debug("docling: TableStructureOptions unavailable, using defaults")

        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        # ``page_range`` was added mid-2.x. Probing the signature is cheaper and
        # more reliable than catching TypeError from a real conversion, which
        # could also be raised by something else inside the pipeline.
        try:
            self._page_range_supported = "page_range" in inspect.signature(
                converter.convert
            ).parameters
        except (TypeError, ValueError):
            self._page_range_supported = True
        if not self._page_range_supported:
            logger.warning(
                "docling %s does not support page_range; the whole document window "
                "will be converted and filtered afterwards",
                self.version,
            )
        return converter

    def _get_converter(self, deadline: float | None) -> Any:
        if self._converter is not None:
            return self._converter
        # In the subprocess path there is no deadline (the parent kills the
        # child), so fall back to the configured budget rather than a literal.
        budget = config.timeout_s()
        if deadline is not None:
            budget = max(1.0, deadline - time.monotonic())
        started = time.monotonic()
        self._converter = self._build_converter(budget)
        logger.debug("docling converter ready in %.1fs", time.monotonic() - started)
        return self._converter

    def _convert(self, path: Path, run: tuple[int, int], last_page: int) -> Any:
        """Convert one page window. Must be called with :data:`_CONVERT_LOCK` held."""
        if self._page_range_supported:
            return self._converter.convert(str(path), page_range=run)
        # No page_range: bound the work by the last page the caller asked for
        # instead of converting a 300-page annual report to read page 12.
        return self._converter.convert(str(path), max_num_pages=last_page)

    # -- engine protocol ---------------------------------------------------

    def extract(
        self,
        path: Path,
        pages: list[int],
        *,
        max_tables: int = 20,
        deadline: float | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> RawBatch:
        batch = RawBatch()
        if not pages:
            return batch

        wanted = set(pages)
        total = len(pages)
        last_page = max(pages)
        done = 0
        first_error: Exception | None = None

        with _CONVERT_LOCK:
            self._get_converter(deadline)

            # Without page_range support a single whole-window conversion serves
            # every page; with it, one conversion per contiguous run.
            windows = page_runs(pages) if self._page_range_supported else [(1, last_page)]

            for run in windows:
                if deadline is not None and time.monotonic() > deadline:
                    batch.partial = True
                    break
                try:
                    result = self._convert(path, run, last_page)
                except Exception as exc:
                    first_error = first_error or exc
                    logger.warning("docling failed on pages %s-%s: %s", run[0], run[1], exc)
                    done += sum(1 for p in pages if run[0] <= p <= run[1])
                    if progress:
                        progress(done, total)
                    continue

                doc = getattr(result, "document", None)
                if doc is None:
                    continue

                for table in getattr(doc, "tables", None) or []:
                    page = table_page(table)
                    if page is None:
                        page = run[0]
                    elif page < run[0]:
                        # Some backends renumber pages when a page_range is
                        # given; absolute numbers are never below the run start.
                        page += run[0] - 1
                    if page not in wanted:
                        continue

                    raw = build_raw_table(table, doc, page=page)
                    if raw is None:
                        continue
                    batch.tables.append(raw)
                    if len(batch.tables) >= max_tables:
                        batch.partial = True
                        break

                batch.pages_read.extend(sorted(p for p in pages if run[0] <= p <= run[1]))
                done += sum(1 for p in pages if run[0] <= p <= run[1])
                if progress:
                    progress(done, total)
                if len(batch.tables) >= max_tables:
                    break

        if not batch.tables and first_error is not None:
            raise first_error
        return batch
