"""Data structures for PDF table extraction.

Two layers:

* :class:`RawTable` — what an engine returns: a rectangular grid of
  :class:`Cell` plus provenance (page, bbox, caption).
* :class:`TableRecord` — the normalised, LLM-facing record: flattened column
  names, inferred column types, unit scale, and both markdown + CSV renderings.

The two layers are kept separate so that an engine (heuristic or docling) only
has to produce geometry, while every finance-specific rule lives in
:mod:`src.tools.tables.normalize` and can be unit-tested without a PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Bump whenever normalisation semantics change. Included in the cache key, so a
# bump invalidates every cached extraction (old records would be inconsistent).
# "2": cross-page stitching — records now carry a `pages` span.
NORMALIZE_VERSION = "2"


@dataclass
class Cell:
    """One grid cell."""

    text: str = ""
    row_span: int = 1
    col_span: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "row_span": self.row_span, "col_span": self.col_span}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Cell:
        return cls(
            text=str(data.get("text", "")),
            row_span=int(data.get("row_span", 1) or 1),
            col_span=int(data.get("col_span", 1) or 1),
        )


@dataclass
class RawTable:
    """A table as produced by an extraction engine (no finance semantics)."""

    page: int  # 1-based page number of the first page the table appears on
    bbox: tuple[float, float, float, float]  # (left, bottom, right, top)
    cells: list[list[Cell]] = field(default_factory=list)
    header_rows: int = 1
    caption: str = ""
    confidence: float | None = None
    # Every page this table occupies. One entry normally; more once the table
    # has been stitched across a page break (see ``tables/continuation.py``).
    pages: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pages:
            self.pages = [self.page]

    @property
    def n_rows(self) -> int:
        return len(self.cells)

    @property
    def n_cols(self) -> int:
        return max((len(row) for row in self.cells), default=0)

    def row_texts(self) -> list[list[str]]:
        """Return the grid as plain strings (ragged rows preserved)."""
        return [[c.text for c in row] for row in self.cells]

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the subprocess worker's JSON channel."""
        return {
            "page": self.page,
            "pages": list(self.pages),
            "bbox": list(self.bbox),
            "cells": [[c.to_dict() for c in row] for row in self.cells],
            "header_rows": self.header_rows,
            "caption": self.caption,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawTable:
        bbox = tuple(data.get("bbox") or (0.0, 0.0, 0.0, 0.0))
        if len(bbox) != 4:
            bbox = (0.0, 0.0, 0.0, 0.0)
        page = int(data.get("page", 0) or 0)
        pages = [int(p) for p in (data.get("pages") or [])] or [page]
        return cls(
            page=page,
            bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            cells=[
                [Cell.from_dict(c) for c in row] for row in (data.get("cells") or [])
            ],
            header_rows=int(data.get("header_rows", 1) or 0),
            caption=str(data.get("caption", "")),
            confidence=data.get("confidence"),
            pages=pages,
        )


@dataclass
class TableRecord:
    """A normalised table ready to hand to the model."""

    id: str
    page: int
    caption: str
    header_rows: int
    columns: list[str]
    column_types: list[str]
    rows: list[list[str]]
    unit_label: str | None = None
    unit_scale: float | None = None
    statement_hint: str | None = None
    markdown: str = ""
    csv: str = ""
    truncated: bool = False
    confidence: float | None = None
    # Full page span; ``page`` is always ``pages[0]``. More than one entry means
    # the table was stitched across a page break.
    pages: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pages:
            self.pages = [self.page]

    @property
    def n_rows(self) -> int:
        """Total row count including header rows."""
        return len(self.rows) + self.header_rows

    @property
    def n_cols(self) -> int:
        return len(self.columns)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the tool envelope / cache."""
        return {
            "id": self.id,
            "page": self.page,
            "pages": list(self.pages),
            "caption": self.caption,
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "header_rows": self.header_rows,
            "columns": list(self.columns),
            "column_types": list(self.column_types),
            "rows": [list(r) for r in self.rows],
            "unit_label": self.unit_label,
            "unit_scale": self.unit_scale,
            "statement_hint": self.statement_hint,
            "markdown": self.markdown,
            "csv": self.csv,
            "truncated": self.truncated,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableRecord:
        """Rebuild from cache. Tolerates missing optional keys."""
        page = int(data.get("page", 0))
        return cls(
            id=str(data.get("id", "t0")),
            page=page,
            caption=str(data.get("caption", "")),
            header_rows=int(data.get("header_rows", 1)),
            columns=list(data.get("columns") or []),
            column_types=list(data.get("column_types") or []),
            rows=[list(r) for r in (data.get("rows") or [])],
            unit_label=data.get("unit_label"),
            unit_scale=data.get("unit_scale"),
            statement_hint=data.get("statement_hint"),
            markdown=str(data.get("markdown", "")),
            csv=str(data.get("csv", "")),
            truncated=bool(data.get("truncated", False)),
            confidence=data.get("confidence"),
            pages=[int(p) for p in (data.get("pages") or [])] or [page],
        )


@dataclass
class RawBatch:
    """What an engine returns for one document call."""

    tables: list[RawTable] = field(default_factory=list)
    pages_read: list[int] = field(default_factory=list)
    partial: bool = False


@dataclass
class ExtractionResult:
    """Everything an engine produced for one document call."""

    tables: list[TableRecord]
    engine: str
    engine_version: str | None
    pages_read: list[int]
    partial: bool = False
