"""Cheap per-page table detection, shared with :mod:`doc_reader_tool`.

``read_document`` uses this to set ``has_tables_hint`` so the agent knows a
follow-up :func:`extract_tables` call is worth the extra latency. It runs the
same geometry as the heuristic engine, so the hint never claims a table the
engine cannot actually extract.

Cost is a few tens of milliseconds per sampled page and no model, which is why
sampling (default 5 pages) is acceptable on a hot path.
"""

from __future__ import annotations

import logging
from typing import Sequence

from src.tools.tables.geometry import detect_grid, looks_like_table, read_text_rects

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_LIMIT = 5


def _evenly_spaced(values: Sequence[int], limit: int) -> list[int]:
    """Pick up to ``limit`` values spread evenly across ``values``."""
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[0]]
    step = (len(values) - 1) / (limit - 1)
    return [values[round(i * step)] for i in range(limit)]


def page_has_table(page) -> bool:
    """True when this page's geometry looks like it holds a data table."""
    try:
        rects = read_text_rects(page)
    except Exception as exc:  # malformed page must not break the caller
        logger.debug("table hint: unreadable page: %s", exc)
        return False
    return looks_like_table(detect_grid(rects))


def detect_table_pages(
    doc,
    page_numbers: Sequence[int],
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> list[int]:
    """Return the sampled 1-based page numbers that look like data tables.

    Args:
        doc: An open ``pypdfium2.PdfDocument``.
        page_numbers: Candidate 1-based pages (the caller already parsed any
            requested range, so this never re-implements page parsing).
        limit: Maximum pages to sample. Sampling is even across the range so a
            table on page 12 of a 20-page report is not missed just because the
            first five pages are prose.

    Returns:
        Hit page numbers, ascending. Empty on any failure.
    """
    hits: list[int] = []
    try:
        page_count = len(doc)
    except Exception:
        return hits

    for page_no in _evenly_spaced(list(page_numbers), limit):
        if not 1 <= page_no <= page_count:
            continue
        try:
            page = doc[page_no - 1]
        except Exception:
            continue
        try:
            if page_has_table(page):
                hits.append(page_no)
        finally:
            try:
                page.close()
            except Exception:
                pass
    return hits
