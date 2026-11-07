"""Finance-aware normalisation of raw extracted tables.

These rules are the part no extraction engine provides: parenthesised
negatives, thousands separators, unit rows ("单位：亿元"), multi-row header
flattening, placeholder symbols, column typing, and statement classification.

Everything here is a pure function over strings — no PDF, no I/O, no models —
so every rule is unit-testable with a fixed fixture.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from src.tools.tables.models import Cell, RawTable, TableRecord

# Placeholder tokens that mean "no value". A-share filings use all of these.
_EMPTY_TOKENS = {
    "",
    "-",
    "--",
    "---",
    "—",
    "——",
    "–",
    "－",
    "/",
    "…",
    "...",
    "n/a",
    "na",
    "n.a.",
    "不适用",
    "无",
    "—",
}

# Longest-first so "百万元" wins over "万元" and "元".
_UNIT_SCALES: tuple[tuple[str, float], ...] = (
    ("百万元", 1e6),
    ("千万元", 1e7),
    ("亿元", 1e8),
    ("万元", 1e4),
    ("千元", 1e3),
    ("百万", 1e6),
    ("亿", 1e8),
    ("万", 1e4),
    ("元", 1.0),
)

_UNIT_RE = re.compile(r"单位\s*[:：\s]\s*([亿万千百元%]+)")

# Prefixes that qualify a number without changing it materially ("约 1,200").
_NUM_PREFIXES = ("约", "≈", "~", "≥", "≤", ">", "<", "+", "＋")

_DATE_RE = re.compile(
    r"^(\d{4}[-/]\d{1,2}([-/]\d{1,2})?|\d{4}年(\d{1,2}月)?|\d{1,2}/\d{1,2}/\d{2,4})$"
)

_STATEMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "income": (
        "营业收入",
        "营业成本",
        "净利润",
        "毛利率",
        "营业利润",
        "归属于母公司",
        "销售费用",
        "管理费用",
        "研发费用",
    ),
    "balance": (
        "总资产",
        "总负债",
        "资产负债率",
        "所有者权益",
        "货币资金",
        "存货",
        "应收账款",
    ),
    "cashflow": (
        "经营活动现金流",
        "投资活动现金流",
        "筹资活动现金流",
        "现金及现金等价物",
        "经营性现金流",
    ),
}

_SOURCE_ROW_RE = re.compile(
    r"^(资料来源|数据来源|注\s*[:：]|单位\s*[:：]|source\s*[:：])",
    re.IGNORECASE,
)


def clean_cell(text: str) -> str:
    """Collapse whitespace and strip zero-width characters."""
    if not text:
        return ""
    cleaned = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_number(text: str) -> float | None:
    """Parse a finance-formatted number, or return None if it is not numeric.

    Handles parenthesised negatives ``(1,234.5)``, thousands separators,
    percent signs, unit suffixes (``1.2亿``), placeholder dashes, and the
    qualifier prefixes common in broker reports (``约``, ``≈``, ``>=``).
    """
    raw = clean_cell(text)
    if not raw:
        return None
    if raw.lower() in _EMPTY_TOKENS:
        return None

    t = raw.replace("\uff0c", ",").replace("\uff05", "%")
    t = t.replace("\u2212", "-").replace("\u2013", "-").replace("\uff0d", "-")

    for prefix in _NUM_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()

    negative = False
    if (t.startswith("(") and t.endswith(")")) or (t.startswith("\uff08") and t.endswith("\uff09")):
        negative = True
        t = t[1:-1].strip()

    scale = 1.0
    if t.endswith("%"):
        scale = 0.01
        t = t[:-1].strip()

    for unit, unit_scale in _UNIT_SCALES:
        if t.endswith(unit):
            scale *= unit_scale
            t = t[: -len(unit)].strip()
            break

    t = t.replace(",", "").strip()
    if not t or t in ("-", "+"):
        return None
    try:
        value = float(t)
    except ValueError:
        return None
    value *= scale
    return -value if negative else value


def is_numeric_cell(text: str) -> bool:
    """True when the cell parses as a number."""
    return parse_number(text) is not None


def detect_unit(caption: str, rows: Sequence[Sequence[str]]) -> tuple[str | None, float | None]:
    """Find a ``单位：亿元`` declaration in the caption or the first two rows.

    Returns:
        ``(label, scale)`` e.g. ``("亿元", 1e8)``, or ``(None, None)``.
    """
    candidates: list[str] = [caption or ""]
    for row in rows[:2]:
        candidates.extend(row)

    for text in candidates:
        if not text:
            continue
        match = _UNIT_RE.search(text)
        if not match:
            continue
        token = match.group(1)
        for unit, scale in _UNIT_SCALES:
            if token == unit or token.startswith(unit):
                return unit, scale
    return None, None


def _fill_merged_header(row: Sequence[str]) -> list[str]:
    """Forward-fill blanks in a header row to materialise merged cells.

    In a rendered PDF a merged header cell ("2023A" spanning two sub-columns)
    appears once and the covered cells come back empty. Filling from the
    nearest non-empty cell to the left reproduces the colspan semantics; a
    leading blank (the row-label column) is never filled.
    """
    out = [clean_cell(c) for c in row]
    last = ""
    for idx, value in enumerate(out):
        if value:
            last = value
        elif last:
            out[idx] = last
    return out


def flatten_columns(header_rows: Sequence[Sequence[str]]) -> list[str]:
    """Flatten one or more header rows into a single column-name list.

    A two-row header becomes ``父列/子列``. Blanks inside a header row are
    forward-filled first (merged cells), and repeated values down a column are
    collapsed.
    """
    if not header_rows:
        return []
    width = max((len(r) for r in header_rows), default=0)
    filled = [_fill_merged_header(r) for r in header_rows]
    columns: list[str] = []
    for idx in range(width):
        parts: list[str] = []
        for row in filled:
            value = row[idx] if idx < len(row) else ""
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        columns.append("/".join(parts) if parts else f"col{idx + 1}")
    return columns


def infer_column_type(values: Iterable[str]) -> str:
    """Classify a column as ``number`` / ``date`` / ``empty`` / ``text``."""
    cells = [clean_cell(v) for v in values]
    non_empty = [c for c in cells if c and c.lower() not in _EMPTY_TOKENS]
    if not non_empty:
        return "empty"
    numeric = sum(1 for c in non_empty if parse_number(c) is not None)
    if numeric >= 3 and numeric / len(non_empty) >= 0.6:
        return "number"
    dates = sum(1 for c in non_empty if _DATE_RE.match(c))
    if dates >= 2 and dates / len(non_empty) >= 0.6:
        return "date"
    return "text"


def detect_statement(columns: Sequence[str], first_column: Sequence[str]) -> str | None:
    """Classify the table as an income / balance / cashflow statement, if it is one."""
    haystack = " ".join(columns) + " " + " ".join(first_column)
    best: tuple[int, str] | None = None
    for name, keywords in _STATEMENT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in haystack)
        if hits >= 2 and (best is None or hits > best[0]):
            best = (hits, name)
    return best[1] if best else None


def is_noise_row(row: Sequence[str]) -> bool:
    """True for rows that carry no table data (source notes, page numbers)."""
    cells = [clean_cell(c) for c in row]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return True
    if len(non_empty) == 1:
        only = non_empty[0]
        if _SOURCE_ROW_RE.match(only):
            return True
        # A bare page number sitting inside the grid.
        if re.fullmatch(r"\d{1,3}", only):
            return True
    joined = " ".join(non_empty)
    if _SOURCE_ROW_RE.match(joined) and len(non_empty) <= 2:
        return True
    return False


def _pad(row: Sequence[str], width: int) -> list[str]:
    out = [clean_cell(c) for c in row][:width]
    out.extend([""] * (width - len(out)))
    return out


def render_markdown(columns: Sequence[str], rows: Sequence[Sequence[str]], max_chars: int) -> tuple[str, bool]:
    """Render a GitHub-flavoured markdown table, clipping to ``max_chars``."""

    def esc(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    header = "| " + " | ".join(esc(c) for c in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, divider]
    used = len(header) + len(divider)
    truncated = False
    for row in rows:
        line = "| " + " | ".join(esc(c) for c in row) + " |"
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), truncated


def render_csv(columns: Sequence[str], rows: Sequence[Sequence[str]], max_chars: int) -> tuple[str, bool]:
    """Render tidy CSV, clipping to ``max_chars``."""

    def esc(value: str) -> str:
        if any(ch in value for ch in (",", '"', "\n")):
            return '"' + value.replace('"', '""') + '"'
        return value

    lines = [",".join(esc(c) for c in columns)]
    used = len(lines[0])
    truncated = False
    for row in rows:
        line = ",".join(esc(c) for c in row)
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), truncated


def _drop_empty_columns(
    header_rows: list[list[str]], body: list[list[str]]
) -> tuple[list[list[str]], list[list[str]]]:
    """Drop columns that are empty in both the header and every body row."""
    if not body and not header_rows:
        return header_rows, body
    width = max(
        [len(r) for r in header_rows] + [len(r) for r in body] + [0],
        default=0,
    )
    keep: list[int] = []
    for idx in range(width):
        has_header = any(idx < len(r) and clean_cell(r[idx]) for r in header_rows)
        has_body = any(idx < len(r) and clean_cell(r[idx]) for r in body)
        if has_header or has_body:
            keep.append(idx)
    if not keep:
        return header_rows, body
    new_headers = [[r[i] for i in keep if i < len(r)] for r in header_rows]
    new_body = [[r[i] for i in keep if i < len(r)] for r in body]
    return new_headers, new_body


def normalize_table(
    raw: RawTable,
    *,
    table_id: str,
    max_chars: int = 3000,
) -> TableRecord | None:
    """Turn an engine's :class:`RawTable` into a :class:`TableRecord`.

    Returns ``None`` when the table carries no usable rows after cleaning.
    """
    header_count = max(0, min(raw.header_rows, len(raw.cells)))
    header_rows = [_pad(r, raw.n_cols) for r in raw.row_texts()[:header_count]]
    body_rows = [_pad(r, raw.n_cols) for r in raw.row_texts()[header_count:]]

    header_rows, body_rows = _drop_empty_columns(header_rows, body_rows)

    # Read the unit declaration BEFORE dropping noise rows: the declaration
    # usually *is* one of those rows ("单位：亿元"), and dropping it first would
    # lose the only place the scale is stated.
    unit_label, unit_scale = detect_unit(raw.caption, body_rows)

    body_rows = [r for r in body_rows if not is_noise_row(r)]
    if not header_rows and not body_rows:
        return None
    if len(body_rows) < 1:
        return None

    columns = flatten_columns(header_rows)
    width = len(columns) if columns else max((len(r) for r in body_rows), default=0)
    if width == 0:
        return None
    if not columns:
        columns = [f"col{i + 1}" for i in range(width)]
    columns = columns[:width]

    body_rows = [r[:width] + [""] * (width - len(r[:width])) for r in body_rows]

    first_col = [r[0] for r in body_rows if r]
    column_types = [
        infer_column_type([r[i] for r in body_rows if i < len(r)]) for i in range(width)
    ]

    markdown, md_trunc = render_markdown(columns, body_rows, max_chars)
    csv_text, csv_trunc = render_csv(columns, body_rows, max_chars)

    return TableRecord(
        id=table_id,
        page=raw.page,
        caption=clean_cell(raw.caption)[:200],
        header_rows=len(header_rows),
        columns=columns,
        column_types=column_types,
        rows=body_rows,
        unit_label=unit_label,
        unit_scale=unit_scale,
        statement_hint=detect_statement(columns, first_col),
        markdown=markdown,
        csv=csv_text,
        truncated=md_trunc or csv_trunc,
        confidence=raw.confidence,
        pages=list(raw.pages),
    )


def cells_to_raw(
    grid: list[list[str]],
    *,
    page: int,
    header_rows: int,
    caption: str,
    bbox: tuple[float, float, float, float],
    confidence: float | None = None,
) -> RawTable:
    """Build a :class:`RawTable` from a plain string grid."""
    return RawTable(
        page=page,
        bbox=bbox,
        cells=[[Cell(text=c) for c in row] for row in grid],
        header_rows=header_rows,
        caption=caption,
        confidence=confidence,
    )
