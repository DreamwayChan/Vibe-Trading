"""Stitch tables that continue across a page break.

A table cut by a page break is the most common structural defect in research
report extraction: an annual report's balance sheet routinely runs over two
pages, and without stitching the model receives two half statements — the
second one with no column names at all, so its rows cannot be attributed.

Only two patterns are merged, and both need positive evidence:

* **Repeated header** — Word / WPS "repeat header row on each page" is standard
  practice in Chinese finance documents, so the continuation page carries the
  same header rows. Matching them cell by cell is strong evidence.
* **Continuation caption** — ``表 3（续）`` / ``Continued`` while the header is
  not repeated (hand-typed tables lose the repeat flag when exported).

A headerless, captionless block on the next page is deliberately **not**
merged. It might be a continuation, but it might equally be an unrelated table,
and a wrong merge silently splices unrelated rows into one record — strictly
worse than two honest halves, because nothing downstream can notice.

Runs on :class:`RawTable`, i.e. before normalisation: only at that layer can the
duplicate header rows be dropped, and the merged grid then goes through
``normalize_table`` once, so column types, unit scale, statement hint and the
character budget all see the whole table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.tools.tables.models import Cell, RawTable

# One-column blocks are never stitched: geometrically they are indistinguishable
# from a column of body text, and a "continued" single column carries almost no
# structure worth recovering. Two columns stay eligible because the 项目 | 金额
# shape is a legitimate table (and a common one in A-share filings); the repeated
# header still has to match before anything merges.
MIN_COLS = 2

# "续" only counts when it is a continuation marker, not part of a word such as
# 持续经营 or 连续三年. Requiring brackets / 表 / explicit English avoids that.
_CONTINUATION_RE = re.compile(
    r"（续）|\(续\)|续表|续上表|接上页|continued|cont'd|\(cont\b",
    re.IGNORECASE,
)

# Cells carry kerning artifacts and the occasional full-width space; comparing
# raw text would miss genuine repeats. Whitespace is the only thing collapsed —
# punctuation and digits must still match.
_SPACE_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACE_RE.sub("", text).casefold()


def _row_key(row: list[str]) -> tuple[str, ...]:
    return tuple(_norm(cell) for cell in row)


def _is_continuation_caption(caption: str) -> bool:
    return bool(caption) and bool(_CONTINUATION_RE.search(caption))


def _header_repeat(prev: RawTable, nxt: RawTable) -> int:
    """Rows of ``nxt`` that duplicate ``prev``'s header, to be dropped.

    Full repeat is counted first (the normal case). Failing that, a single
    header row matching ``prev``'s *last* header row still counts: multi-row
    headers lose their merged parent row when re-rendered on a continuation
    page, and the leaf row is the one that actually names the columns.
    """
    header = prev.row_texts()[: prev.header_rows]
    if not header:
        return 0

    body = nxt.row_texts()
    matched = 0
    for want, got in zip(header, body):
        if _row_key(want) != _row_key(got):
            break
        matched += 1
    if matched:
        return matched

    if len(body) >= 1 and _row_key(header[-1]) == _row_key(body[0]):
        return 1
    return 0


def _drop_count(prev: RawTable, nxt: RawTable, min_cols: int) -> int | None:
    """Number of leading rows to discard from ``nxt``, or ``None`` for no merge."""
    # Physical adjacency, measured against the table's LAST page: a chain that
    # already covers 9-10 must still accept 11, and comparing ``page`` would
    # compare 11 to 9. A sparse page selection must not turn a gap into a
    # continuation either — two tables three pages apart are different tables
    # even when their headers coincide.
    if nxt.page != prev.pages[-1] + 1:
        return None
    if prev.n_cols < min_cols or nxt.n_cols != prev.n_cols:
        return None

    repeated = _header_repeat(prev, nxt)
    if repeated:
        return repeated
    if _is_continuation_caption(nxt.caption):
        # Header not repeated, so nothing of ``nxt`` may be dropped: its first
        # row is already data.
        return 0
    return None


def _concat(prev: RawTable, nxt: RawTable, drop: int) -> RawTable:
    width = prev.n_cols
    rows = [list(row) for row in prev.cells]
    for row in nxt.cells[drop:]:
        fitted = list(row[:width])
        # A continuation page occasionally renders one column tag short (the
        # trailing empty column is not drawn). Right-pad rather than let a
        # later ``_pad`` in normalise silently shift cells one column left.
        fitted.extend(Cell() for _ in range(width - len(fitted)))
        rows.append(fitted)
    return RawTable(
        page=prev.page,
        bbox=prev.bbox,
        cells=rows,
        header_rows=prev.header_rows,
        caption=prev.caption,
        confidence=prev.confidence,
        pages=[*prev.pages, *nxt.pages],
    )


@dataclass
class MergeReport:
    """Outcome of a stitching pass, for the envelope and for tests."""

    joins: int = 0
    # Page spans that were stitched, e.g. ``[[4, 5], [9, 10, 11]]``.
    chains: list[list[int]] = field(default_factory=list)

    @property
    def stitched(self) -> bool:
        return self.joins > 0


def merge_continuations(
    tables: list[RawTable],
    *,
    min_cols: int = MIN_COLS,
) -> tuple[list[RawTable], MergeReport]:
    """Stitch adjacent-page continuations into single tables.

    Args:
        tables: Raw tables in engine order (ascending page). Order is relied on:
            stitching only ever looks at the previous output table.
        min_cols: Narrower tables are never stitched.

    Returns:
        ``(merged, report)``. The input list is never mutated; the returned
        tables are either the original objects or a new merged one.
    """
    merged: list[RawTable] = []
    report = MergeReport()

    for raw in tables:
        drop = _drop_count(merged[-1], raw, min_cols) if merged else None
        if drop is None:
            merged.append(raw)
            continue
        merged[-1] = _concat(merged[-1], raw, drop)
        report.joins += 1

    # Derived from the result rather than tracked while merging: incremental
    # bookkeeping duplicated the page number on three-page chains.
    report.chains = [list(t.pages) for t in merged if len(t.pages) > 1]
    return merged, report
