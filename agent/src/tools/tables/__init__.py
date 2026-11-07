"""PDF table extraction: geometry-driven detection + finance-aware normalisation.

Public surface:

* :func:`extract_tables` — orchestration used by the ``extract_tables`` tool.
* :func:`detect_table_pages` — cheap per-page hint shared with ``read_document``.
* :class:`TableRecord` / :class:`RawTable` — the two data layers.

Design notes and measured behaviour live in ``docs/design/research-report-tables.md``.
"""

from __future__ import annotations

from src.tools.tables.hint import detect_table_pages
from src.tools.tables.models import NORMALIZE_VERSION, RawTable, TableRecord
from src.tools.tables.service import extract_tables

__all__ = [
    "NORMALIZE_VERSION",
    "RawTable",
    "TableRecord",
    "detect_table_pages",
    "extract_tables",
]
