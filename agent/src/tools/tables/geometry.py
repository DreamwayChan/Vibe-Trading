"""Geometry primitives shared by the table hint and the heuristic engine.

Everything works on pypdfium2 *text rectangles*. One rect is one text **run**,
not one cell, and how many runs a cell arrives in depends entirely on the PDF
producer:

* a hand-built or export-tool PDF gives ~1 rect per cell;
* a Word/WPS export or a government form gives **1 rect per glyph**, so a prose
  line is 40+ rects and a 3-cell table row is 3.

The second case broke the first implementation of this module (design doc §17),
so the pipeline never looks at rect counts directly:

1. ``band_rows`` groups rects into visual rows by y.
2. ``_band_cells`` merges each row's fragments into cells **per row**, which is
   what makes glyph-level runs and one-run-per-cell PDFs behave the same.
3. ``_table_runs`` finds row ranges whose rows hold enough cells, padding the
   ends because a table's header rows are narrower than its body.
4. ``_corridor_edges`` derives column boundaries from whitespace corridors that
   survive a budget of crossing rows, so merged cells abstain instead of
   collapsing the grid.
5. ``_grid_from_run`` places every cell, then a window search over each run picks
   the sub-window that scores best on size, numeric share and fill.

The module is pure geometry over plain float tuples, so every rule is unit
testable without a PDF file. ``tests/test_table_realworld.py`` holds the cases
taken from the two real documents that motivated the shape of this pipeline.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from src.tools.tables.normalize import clean_cell, is_numeric_cell

# A candidate table body needs at least this many rows / columns / cells per row.
MIN_ROWS = 3
MIN_COLS = 3
MIN_CELLS_PER_ROW = 3

# A whitespace corridor narrower than this is a gap *inside* a cell (a cell split
# into several text runs measured 1.1 pt), not a column separator (~48 pt).
MIN_SEP_GAP = 2.5
SEP_GAP_CHAR_RATIO = 0.5

# A column must be occupied in at least this share of rows, otherwise it is
# paragraph text that happened to align, not a real column.
COL_FILL_RATIO = 0.6

# Rows a run is padded by at both ends, and the number of consecutive
# non-tabular rows tolerated inside it. A table's header rows carry fewer cells
# than its body (a merged header has 2 where the body has 7), so the start has to
# reach back over them; a merged section header ("参保险种情况") does the same
# mid-table. Prose blocks are excluded by the window scoring, not by this value.
MAX_BRIDGE_ROWS = 3

# A corridor crossed by up to this share of the run's rows still separates two
# columns: merged headers cross their own column boundary, prose crosses every
# corridor on the page.
CROSSING_BUDGET_RATIO = 0.3

# Prose is rejected by cell size. Measured: prose fragments on a real page were
# 300-550 pt wide (55-100% of the text width) and carried 40+ characters, while
# table cells in the same report were 10-190 pt (2-35%). A "table" whose median
# cell fills this much of the row is a paragraph that happens to wrap.
PROSE_CELL_WIDTH_RATIO = 0.4

# A cell wider than this multiple of the run's median cell width is treated as
# spanning several columns, so it is anchored on its left edge.
MERGED_CELL_RATIO = 1.5


@dataclass(frozen=True)
class TextRect:
    """One text run's bounding box (PDF canvas units, y grows upward)."""

    left: float
    bottom: float
    right: float
    top: float
    text: str

    @property
    def x_center(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def y_center(self) -> float:
        return (self.bottom + self.top) / 2.0

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom


@dataclass
class GridResult:
    """A detected table: cell text grid plus light provenance."""

    cells: list[list[str]] = field(default_factory=list)
    header_rows: int = 0
    caption: str = ""
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    numeric_ratio: float = 0.0

    @property
    def n_rows(self) -> int:
        return len(self.cells)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.cells), default=0)

    @property
    def score(self) -> int:
        return self.n_rows * self.n_cols


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def read_text_rects(page) -> list[TextRect]:
    """Extract text rectangles from a pypdfium2 page.

    ``count_rects()`` must run before ``get_rect()`` — pypdfium2 returns empty
    boxes otherwise **without raising**, which would silently yield "no tables".
    """
    textpage = page.get_textpage()
    try:
        count = textpage.count_rects()
        if not count:
            return []
        rects: list[TextRect] = []
        for idx in range(count):
            left, bottom, right, top = textpage.get_rect(idx)
            text = clean_cell(textpage.get_text_bounded(left, bottom, right, top))
            if text:
                rects.append(TextRect(left, bottom, right, top, text))
        return rects
    finally:
        textpage.close()


def band_rows(rects: Sequence[TextRect], *, tol: float | None = None) -> list[list[TextRect]]:
    """Group rects into horizontal bands (visual rows), top of page first."""
    if not rects:
        return []
    if tol is None:
        heights = [r.height for r in rects if r.height > 0]
        # Half a median glyph height: tolerant of baseline jitter, tight enough
        # that adjacent 8 pt rows never collapse into one band.
        tol = max(2.0, 0.5 * _median(heights)) if heights else 4.0

    ordered = sorted(rects, key=lambda r: -r.y_center)
    bands: list[list[TextRect]] = [[ordered[0]]]
    for rect in ordered[1:]:
        band_mean = sum(r.y_center for r in bands[-1]) / len(bands[-1])
        if abs(band_mean - rect.y_center) <= tol:
            bands[-1].append(rect)
        else:
            bands.append([rect])
    return bands


def _median_char_width(rects: Sequence[TextRect]) -> float:
    widths = [r.width / max(len(r.text), 1) for r in rects if r.text and r.width > 0]
    return _median(widths) if widths else 4.0


@dataclass(frozen=True)
class _BandCell:
    """One visual cell: the fragment rects of a single row, joined on x."""

    left: float
    right: float
    rects: tuple[TextRect, ...]

    @property
    def width(self) -> float:
        return self.right - self.left


def _band_cells(band: Sequence[TextRect], min_gap: float) -> list[_BandCell]:
    """Group one row's rects into cells.

    Fragments are merged **per row**, not across the run. This is the fix for
    the measured real-document failure: a CJK PDF emits one rect per glyph, so
    counting rects in a row says nothing about whether the row is tabular — a
    prose line can carry 45 rects while a 3-cell table row carries 3. Merged
    here, prose collapses to one wide cell and a table row stays several.
    """
    ordered = sorted(band, key=lambda r: r.left)
    groups: list[list[TextRect]] = [[ordered[0]]]
    for rect in ordered[1:]:
        if rect.left - max(r.right for r in groups[-1]) <= min_gap:
            groups[-1].append(rect)
        else:
            groups.append([rect])
    return [_BandCell(g[0].left, max(r.right for r in g), tuple(g)) for g in groups]


def _table_runs(rows: Sequence[Sequence[_BandCell]]) -> list[tuple[int, int]]:
    """Find contiguous row ranges that look like table bodies.

    A row is tabular when it holds at least ``MIN_CELLS_PER_ROW`` cells. The
    range is then padded by ``MAX_BRIDGE_ROWS`` on both sides, because a table's
    **header rows are narrower than its body**: a merged header carries 2 cells
    where the body has 7. Padding is deliberately generous — the caller scores
    every sub-window, so extra rows cost nothing and a lost header costs names.

    Two consecutive non-tabular rows still end the range, which is what keeps a
    prose block from being absorbed into the neighbouring table.
    """
    runs: list[tuple[int, int]] = []
    start: int | None = None
    last_dense: int | None = None
    misses = 0

    for idx, cells in enumerate(rows):
        if len(cells) >= MIN_CELLS_PER_ROW:
            if start is None:
                start = idx
                # Walk back over header bands, which are narrower than the body
                # (a merged header carries 2 cells where the body has 7). A
                # caption is left to `_grid_from_run`, which demotes it once the
                # grid shows a single cell on its first row and a full row after.
                for back in range(1, MAX_BRIDGE_ROWS + 1):
                    pos = idx - back
                    if pos < 0 or len(rows[pos]) < 2:
                        break
                    start = pos
            last_dense = idx
            misses = 0
            continue
        if start is None:
            continue
        misses += 1
        if misses > MAX_BRIDGE_ROWS:
            assert last_dense is not None
            runs.append((start, min(len(rows) - 1, last_dense + MAX_BRIDGE_ROWS)))
            start = None
            misses = 0

    if start is not None and last_dense is not None:
        runs.append((start, min(len(rows) - 1, last_dense + MAX_BRIDGE_ROWS)))
    return [(s, e) for s, e in runs if e - s + 1 >= MIN_ROWS]


def _corridor_edges(
    rows: Sequence[Sequence[_BandCell]], min_gap: float, crossing_budget: int
) -> list[float]:
    """Find column boundaries as whitespace corridors that survive the table.

    Three approaches were tried against two real documents; the first two both
    ended in ``table_count: 0`` with ``status: ok``:

    * **Union projection** (P0) — a corridor only counted when no rect crossed
      it, so one full-width cell (a merged header, or one prose line caught in
      the run) collapsed the table to a single column.
    * **Gap-midpoint voting** — the midpoint between a variable-width cell and
      its right neighbour moves by tens of points between rows (a company-name
      column is a different width on every row), so real boundaries never
      gathered enough votes.

    A corridor that up to ``crossing_budget`` rows cross is a real separator:
    the merged header "参保起止时间" crosses its own column boundary, and it is
    still one column. Only a corridor *every* row crosses is body text.
    """
    deltas: dict[float, int] = defaultdict(int)
    for cells in rows:
        for cell in cells:
            deltas[cell.left] += 1
            deltas[cell.right] -= 1

    xs = sorted(deltas)
    if not xs:
        return []

    # Coverage between consecutive event positions, then merge the empty runs.
    segments: list[tuple[float, float, int]] = []
    count = 0
    for idx, x in enumerate(xs):
        count += deltas[x]
        nxt = xs[idx + 1] if idx + 1 < len(xs) else x
        segments.append((x, nxt, count))

    boundaries: list[float] = []
    corridor_start: float | None = None
    corridor_end: float = 0.0
    for start, end, covered in segments:
        if covered <= crossing_budget:
            if corridor_start is None:
                corridor_start = start
            corridor_end = end
            continue
        if corridor_start is not None and corridor_end - corridor_start >= min_gap:
            boundaries.append((corridor_start + corridor_end) / 2.0)
        corridor_start = None
    if corridor_start is not None and corridor_end - corridor_start >= min_gap:
        boundaries.append((corridor_start + corridor_end) / 2.0)

    return boundaries


def _join_run(rects: Sequence[TextRect], merge_gap: float) -> str:
    """Join one cell's fragment rects in x order, inserting a space only on a real gap."""
    parts = [rects[0].text]
    prev = rects[0]
    for rect in rects[1:]:
        gap = rect.left - prev.right
        parts.append(("" if gap <= merge_gap else " ") + rect.text)
        prev = rect
    return clean_cell("".join(parts))


def _detect_header_rows(grid: Sequence[Sequence[str]]) -> int:
    """Return how many leading rows are header rows (0, 1 or 2).

    Two rows is the common real-world shape: a merged parent row ("2024A")
    above a leaf row ("收入 / 占比"), or a vertically merged label ("单位") whose
    sub-columns ("养老 / 工伤 / 失业") only appear on the second band. Detecting
    only one row pushes that leaf band into the data, where its labels become a
    row of stray values.
    """
    if len(grid) < 2:
        return 0
    first = [c for c in grid[0] if c]
    if not first:
        return 0
    if any(is_numeric_cell(c) for c in first):
        return 0
    if not any(is_numeric_cell(c) for row in grid[1:3] for c in row):
        return 0

    second = [c for c in grid[1] if c]
    if second and len(grid) >= 3 and not any(is_numeric_cell(c) for c in second):
        if any(is_numeric_cell(c) for c in grid[2]):
            return 2
    return 1


def _caption_above(bands: Sequence[Sequence[TextRect]], start: int, row_pitch: float) -> str:
    """Text of the single band directly above the table, if it is close enough."""
    if start == 0 or row_pitch <= 0:
        return ""
    above = bands[start - 1]
    if not above:
        return ""
    # y grows upward, so the band above sits at a larger y than the table's top.
    table_top = max(r.y_center for r in bands[start])
    above_bottom = min(r.y_center for r in above)
    if above_bottom - table_top > 3.0 * row_pitch:
        return ""
    text = clean_cell(" ".join(r.text for r in sorted(above, key=lambda r: r.left)))
    # A caption is short; a long band is body text that merely sits above.
    return text[:200] if len(text) <= 200 else ""


def _grid_from_run(
    bands: Sequence[Sequence[TextRect]],
    rows: Sequence[Sequence[_BandCell]],
    start: int,
    end: int,
    min_gap: float,
) -> GridResult | None:
    run_cells = rows[start : end + 1]
    flat = [rect for band in bands[start : end + 1] for rect in band]
    if len(flat) < MIN_ROWS * MIN_COLS:
        return None

    run_width = max(r.right for r in flat) - min(r.left for r in flat)
    cell_widths = [cell.width for cells in run_cells for cell in cells]
    if run_width > 0 and cell_widths:
        if _median(cell_widths) > PROSE_CELL_WIDTH_RATIO * run_width:
            return None

    budget = max(1, math.ceil(CROSSING_BUDGET_RATIO * len(run_cells)))
    edges = _corridor_edges(run_cells, min_gap, budget)
    n_cols = len(edges) + 1
    if n_cols < MIN_COLS:
        return None

    widths = [cell.width for cells in run_cells for cell in cells]
    typical = _median(widths) if widths else 0.0
    join_gap = 0.5 * min_gap

    grid: list[list[str]] = []
    occupancy = [0] * n_cols
    for cells in run_cells:
        line = [""] * n_cols
        for cell in cells:
            # A merged cell is much wider than a normal one. Anchoring it on its
            # left edge keeps a section header in the first column it covers,
            # instead of dropping it in the middle of its span.
            anchor = (
                cell.left
                if typical and cell.width > MERGED_CELL_RATIO * typical
                else (cell.left + cell.right) / 2.0
            )
            col = min(bisect_right(edges, anchor), n_cols - 1)
            text = _join_run(sorted(cell.rects, key=lambda r: r.left), join_gap)
            if not text:
                continue
            line[col] = f"{line[col]} {text}".strip() if line[col] else text
            if line[col] == text:
                occupancy[col] += 1
        grid.append(line)

    keep = [i for i, count in enumerate(occupancy) if count >= COL_FILL_RATIO * len(run_cells)]
    if len(keep) < MIN_COLS:
        return None
    grid = [[row[i] for i in keep] for row in grid]

    # A single cell on the first row above a full row is a caption that the run
    # padding picked up ("表 1：主要财务指标"), not a header: a header row never
    # covers just one column while the row below fills all of them.
    caption = ""
    if len(grid) > MIN_ROWS:
        first_row = [c for c in grid[0] if c]
        second_row = [c for c in grid[1] if c]
        if len(first_row) == 1 and len(second_row) >= MIN_COLS:
            caption = first_row[0][:200]
            grid = grid[1:]
            start += 1

    header_rows = _detect_header_rows(grid)

    # The numeric ratio is measured on the BODY only. Counting header cells
    # made dropping the header raise the score, so the window search optimised
    # the header away: a real three-column table came back as "col1..col3" with
    # its "品类 / 领先品牌 / 市场份额" row gone.
    body = grid[header_rows:] if header_rows < len(grid) else []
    non_empty = [c for row in body for c in row if c]
    numeric_ratio = (
        sum(1 for c in non_empty if is_numeric_cell(c)) / len(non_empty) if non_empty else 0.0
    )

    row_pitch = _median(
        [
            bands[start + i][0].y_center - bands[start + i + 1][0].y_center
            for i in range(end - start)
            if bands[start + i] and bands[start + i + 1]
        ]
    )

    return GridResult(
        cells=grid,
        header_rows=header_rows,
        caption=caption or _caption_above(bands, start, row_pitch),
        bbox=(
            min(r.left for r in flat),
            min(r.bottom for r in flat),
            max(r.right for r in flat),
            max(r.top for r in flat),
        ),
        numeric_ratio=numeric_ratio,
    )


def detect_grid(rects: Sequence[TextRect]) -> GridResult | None:
    """Detect the largest table on a page, or return ``None``.

    Only one table per page is returned: research-report pages rarely hold two
    independent data tables, and returning the largest keeps the failure mode
    predictable (a mis-merge degrades one table, not the whole page).
    """
    bands = band_rows(rects)
    if len(bands) < MIN_ROWS:
        return None

    char_width = _median_char_width(rects)
    min_gap = max(MIN_SEP_GAP, SEP_GAP_CHAR_RATIO * char_width)
    cells_per_row = [_band_cells(band, min_gap) for band in bands]

    best: GridResult | None = None
    best_score = 0.0
    for start, end in _table_runs(cells_per_row):
        # A run is often longer than the table: a footer paragraph or a note can
        # join it. Every sub-window is therefore tried and scored instead of
        # trusting the whole run — on a real form, the whole run included the
        # 备注 block and dragged the numeric ratio below the table threshold.
        for win_start in range(start, end - MIN_ROWS + 2):
            for win_end in range(win_start + MIN_ROWS - 1, end + 1):
                candidate = _grid_from_run(
                    bands, cells_per_row, win_start, win_end, min_gap
                )
                if candidate is None or not looks_like_table(candidate):
                    continue
                # Fill ratio penalises windows that pad the table with a note
                # line: it adds a row and a column but leaves both mostly empty.
                filled = sum(1 for row in candidate.cells for cell in row if cell)
                capacity = candidate.n_rows * candidate.n_cols
                fill = filled / capacity if capacity else 0.0
                score = (
                    candidate.n_rows * candidate.n_cols * candidate.numeric_ratio * fill
                )
                if score > best_score:
                    best, best_score = candidate, score
    return best


def looks_like_table(result: GridResult | None, *, min_numeric_ratio: float = 0.3) -> bool:
    """True when a detected grid is a data table rather than aligned prose."""
    if result is None:
        return False
    if result.n_rows < MIN_ROWS or result.n_cols < MIN_COLS:
        return False
    return result.numeric_ratio >= min_numeric_ratio


_TOC_RE = re.compile(r"[\.\u2026]{3,}\s*\d+$")


def is_toc_band(band: Sequence[TextRect]) -> bool:
    """True for directory / table-of-contents lines (dot leaders + page number)."""
    if not band:
        return False
    joined = " ".join(r.text for r in band)
    return bool(_TOC_RE.search(joined))
