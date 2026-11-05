"""Read-only tool: extract structured tables from a PDF document.

Complements ``read_document``: that tool returns a document's *text*, in which a
table collapses into a column of unrelated numbers; this tool returns the
table's *grid* — column names, inferred column types, unit scale, and a tidy CSV
rendering — so downstream analysis can actually do arithmetic on it.
"""

from __future__ import annotations

from typing import Any

from src.agent.tools import BaseTool
from src.tools.tables import config
from src.tools.tables.service import extract_tables


class ExtractTablesTool(BaseTool):
    """Extract tables (rows × columns) from a PDF as structured data."""

    name = "extract_tables"
    description = (
        "Extract tables from a PDF as structured rows and columns. Use this "
        "instead of read_document when you need the NUMBERS inside tables: "
        "research reports, annual/quarterly reports, and financial statements "
        "lose their row/column alignment when read as plain text. Each returned "
        "table carries its caption, column names, inferred column types, the "
        "declared unit (亿元/万元) with its multiplier, a statement "
        "classification (income/balance/cashflow) when applicable, and both a "
        "markdown and a CSV rendering. A table that continues onto the next "
        "page comes back as ONE record whose `pages` lists the whole span, "
        "with the repeated header dropped. The default engine reads the PDF "
        "text layer; scanned pages need read_document with OCR first. Check the "
        "`quality` field: 'structural' means merged cells and multi-row "
        "headers were read from the document grid, 'geometric' means columns "
        "were inferred from text positions and may be mis-split."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the PDF file.",
            },
            "pages": {
                "type": "string",
                "description": (
                    "1-based page selection, e.g. '1-10', '5', '1,3,5-8'. "
                    "Empty means all pages (bounded by the configured page cap). "
                    "Use the table_pages_hint returned by read_document to target "
                    "only the pages that hold tables."
                ),
                "default": "",
            },
            "max_tables": {
                "type": "integer",
                "description": "Maximum number of tables to return. Default 20.",
                "default": 20,
            },
            "include_csv": {
                "type": "boolean",
                "description": (
                    "Include a tidy CSV rendering per table (default true). "
                    "Set false to save context when markdown is enough."
                ),
                "default": True,
            },
        },
        "required": ["file_path"],
    }
    repeatable = True

    @classmethod
    def check_available(cls) -> bool:
        """Excluded from the registry when table extraction is switched off."""
        return config.table_engine_name() != "off"

    def execute(self, **kwargs: Any) -> str:
        return extract_tables(
            kwargs["file_path"],
            kwargs.get("pages", ""),
            max_tables=kwargs.get("max_tables"),
            include_csv=kwargs.get("include_csv", True),
        )
