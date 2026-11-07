"""Orchestration for the ``extract_tables`` tool.

Flow: validate path → resolve page selection against the real page count →
cache lookup → engine extraction → normalise → budget → envelope.

The envelope is deliberately *separate* from ``read_document``'s: tables need
their own character budget, otherwise the document's 15 000-char text cap
starves them (design doc §3/D1). Both envelopes share field names where the
meaning is the same (``status`` / ``file`` / ``format`` / ``char_count`` /
``truncated``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.agent.progress import emit_progress
from src.security.scanner import with_security_warnings
from src.tools.path_utils import safe_document_path
from src.tools.tables import cache, config
from src.tools.tables.continuation import merge_continuations
from src.tools.tables.engine import (
    TableEngineDisabled,
    TableEngineUnavailable,
    get_table_engine,
)
from src.tools.tables.hint import detect_table_pages
from src.tools.tables.models import TableRecord
from src.tools.tables.normalize import normalize_table
from src.tools.tables.pages import parse_page_selection

_MAX_TABLE_CHARS = 8000
_MAX_CHARS_PER_TABLE = 3000
_MAX_TABLES = 20

# Below this much remaining budget a table would be unreadable anyway.
_MIN_TABLE_BUDGET = 200

_QUALITY_BY_ENGINE = {"heuristic": "geometric", "docling": "structural"}


def _err(message: str) -> str:
    return json.dumps({"status": "error", "error": message}, ensure_ascii=False)


def _envelope(path: Path, payload: dict[str, Any]) -> str:
    """Attach security warnings to untrusted table content and serialise."""
    secured = with_security_warnings(
        payload,
        fields=("tables.*.markdown", "tables.*.csv", "tables.*.caption"),
    )
    return json.dumps(secured, ensure_ascii=False)


def _page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


def _resolve_pages(path: Path, pages: str) -> tuple[list[int], list[int], bool]:
    """Resolve a page selection to concrete 1-based pages.

    Returns:
        ``(requested, selected, capped)`` — ``requested`` is what the caller
        asked for, ``selected`` is what will actually be read (bounded by
        ``VIBE_TRADING_TABLE_MAX_PAGES``), ``capped`` flags the difference.
    """
    total = _page_count(path)
    requested = parse_page_selection(pages) or list(range(1, total + 1))
    requested = [p for p in requested if 1 <= p <= total]
    if not requested:
        raise ValueError(f"page selection is outside the document (1-{total})")

    cap = config.max_pages()
    if len(requested) <= cap:
        return requested, requested, False

    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        hinted = detect_table_pages(doc, requested, limit=cap)
    finally:
        doc.close()

    selected = hinted[:cap] if hinted else requested[:cap]
    return requested, selected, True


def _collect(
    tables: list,
    *,
    max_tables: int,
) -> tuple[list[TableRecord], bool]:
    """Normalise raw tables under the character budget.

    Returns:
        ``(records, budget_truncated)``.
    """
    records: list[TableRecord] = []
    used = 0
    budget_truncated = False

    for raw in tables:
        if len(records) >= max_tables:
            budget_truncated = True
            break
        remaining = _MAX_TABLE_CHARS - used
        if remaining < _MIN_TABLE_BUDGET:
            budget_truncated = True
            break

        record = normalize_table(
            raw,
            table_id=f"t{len(records) + 1}",
            max_chars=min(_MAX_CHARS_PER_TABLE, remaining),
        )
        if record is None:
            continue
        used += len(record.markdown)
        records.append(record)

    return records, budget_truncated


def extract_tables(
    file_path: str,
    pages: str = "",
    *,
    max_tables: int | None = None,
    include_csv: bool = True,
) -> str:
    """Extract structural tables from a PDF and return a JSON envelope.

    Args:
        file_path: Absolute path to the PDF.
        pages: 1-based page selection (``"1-10"``, ``"5"``, ``"1,3,5-8"``);
            empty means every page.
        max_tables: Cap on returned tables (default 20).
        include_csv: Include a tidy CSV rendering per table.

    Returns:
        JSON envelope string. ``status`` is ``ok`` / ``partial`` / ``error``.
    """
    started = time.monotonic()

    try:
        path = safe_document_path(file_path)
    except ValueError as exc:
        return _err(str(exc))
    if not path.exists():
        return _err(f"File not found: {file_path}")
    if not path.is_file():
        return _err(f"Not a file: {file_path}")
    if path.suffix.lower() != ".pdf":
        return _err(
            f"Table extraction supports PDF only (got {path.suffix or 'no extension'}). "
            "Use read_document for docx/xlsx, which already returns their tables."
        )

    try:
        engine = get_table_engine()
    except TableEngineDisabled as exc:
        return _err(str(exc))
    except TableEngineUnavailable as exc:
        return _err(str(exc))

    if not engine.supports(path):
        return _err(f"engine {engine.name!r} does not support {path.suffix}")

    try:
        requested, selected, capped = _resolve_pages(path, pages)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"Failed to read {path.name}: {type(exc).__name__}: {exc}")

    limit = max_tables if max_tables and max_tables > 0 else _MAX_TABLES
    key = cache.make_key(path, engine.name, engine.version, selected)

    cached = cache.load(key) if config.cache_enabled() else None
    if not config.cache_enabled():
        cache_state = "off"
    elif cached is not None:
        cache_state = "hit"
    else:
        cache_state = "miss"

    records: list[TableRecord]
    budget_truncated = False
    partial = False

    if cached is not None:
        records = cached
    else:
        deadline = started + config.timeout_s()

        def _progress(current: int, total: int) -> None:
            emit_progress(
                "extracting_tables",
                current=current,
                total=total,
                message=f"page {selected[current - 1] if current - 1 < len(selected) else current}",
            )

        try:
            # Always extract up to the module-wide maximum, never the caller's
            # ``max_tables``: the cache is keyed without that value, so a call
            # with a small limit must not poison a later call that wants more.
            batch = engine.extract(
                path,
                selected,
                max_tables=_MAX_TABLES,
                deadline=deadline,
                progress=_progress,
            )
        except Exception as exc:
            return _err(f"Table extraction failed: {type(exc).__name__}: {exc}")

        partial = batch.partial
        # Stitch before normalising: only at the RawTable layer can the
        # duplicated header rows of a continuation page be dropped, and the
        # merged grid must go through the budget and unit rules as one table.
        merged, _ = merge_continuations(batch.tables)
        records, budget_truncated = _collect(merged, max_tables=_MAX_TABLES)
        if records:
            cache.save(key, records)

    if len(records) > limit:
        records = records[:limit]
        budget_truncated = True
    if not include_csv:
        for record in records:
            record.csv = ""

    elapsed_ms = int((time.monotonic() - started) * 1000)
    char_count = sum(len(r.markdown) for r in records)
    truncated = budget_truncated or any(r.truncated for r in records) or capped
    # Derived from the records so a cache hit reports the same span as a fresh run.
    stitched_pages = [list(r.pages) for r in records if len(r.pages) > 1]

    payload: dict[str, Any] = {
        "status": "partial" if partial else "ok",
        "file": path.name,
        "format": "pdf",
        "engine": engine.name,
        "engine_version": engine.version,
        "quality": _QUALITY_BY_ENGINE.get(engine.name, "unknown"),
        "pages_requested": requested,
        "pages_read": selected,
        "table_count": len(records),
        "tables": [r.to_dict() for r in records],
        "char_count": char_count,
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "cache": cache_state,
    }
    if stitched_pages:
        payload["stitched_pages"] = stitched_pages

    if not records:
        payload["note"] = (
            "No table detected in the selected pages. The PDF may be a scanned image "
            "(no text layer) or the tables may have no extractable text; try "
            "read_document with OCR on those pages instead."
        )
    elif capped:
        payload["note"] = (
            f"Page selection limited to {len(selected)} of {len(requested)} pages "
            f"(VIBE_TRADING_TABLE_MAX_PAGES); pass an explicit `pages` range for the rest."
        )
    elif truncated:
        payload["note"] = "Output hit the character budget; later tables or rows were dropped."

    return _envelope(path, payload)
