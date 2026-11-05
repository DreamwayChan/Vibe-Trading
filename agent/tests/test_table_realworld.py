"""Regression tests built from the measured shape of real-world PDFs.

Every other geometry test in this suite builds its rects as **one rect per
cell**. Real documents do not look like that: Word/WPS exports and government
forms emit **one text run per glyph**, so a prose line arrives as 40+ rects while
a 3-cell table row arrives as 3.

That difference is not cosmetic. The first detector counted rects per row as its
table signal and used union projection for columns. Measured on two real
documents it returned ``table_count: 0`` with ``status: ok``:

* a social-security certificate — a genuine 7-column table with merged headers —
  produced nothing at all;
* a research-report page of flowing prose produced a fake 3-column "table".

Rects are built directly here rather than through a PDF: pdfium reports each
glyph's tight ink box, and a Latin font's ink width does not match its advance,
so a synthetic PDF letter-spaces the text and fabricates a column between every
two letters. Constructing :class:`TextRect` gives exact control over the property
under test (run granularity and gap widths) with no font metrics involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.tables.geometry import TextRect, detect_grid, looks_like_table
from src.tools.tables.service import extract_tables
from tests.test_table_extract_tool import write_pdf

# Measured from the real certificate: a CJK advance equals its glyph width, so
# consecutive runs touch and merge into one cell.
CHAR_W = 10.0
ROW_H = 14.0
COL_PITCH = 170.0
X0 = 40.0
Y0 = 760.0


def cell_glyphs(
    text: str, *, left: float, top: float, char_w: float = CHAR_W
) -> list[TextRect]:
    """One rect per character, adjacent (advance == width, as CJK glyphs are).

    A space advances a fraction of a glyph, not a full slot: in a real PDF a
    space is ~0.3 em, and giving it a full slot would open a gap wide enough to
    split one cell into two — a defect of the fixture, not of the detector.
    """
    out: list[TextRect] = []
    x = left
    for char in text:
        if char == " ":
            x += char_w * 0.3
            continue
        out.append(TextRect(x, top - ROW_H, x + char_w, top, char))
        x += char_w
    return out


def table_rects(
    rows: list[list[str]],
    *,
    x0: float = X0,
    y0: float = Y0,
    pitch: float = COL_PITCH,
) -> list[TextRect]:
    rects: list[TextRect] = []
    for r, row in enumerate(rows):
        top = y0 - r * ROW_H
        for c, text in enumerate(row):
            rects.extend(cell_glyphs(text, left=x0 + c * pitch, top=top))
    return rects


TABLE = [
    ["Item", "2024A", "2025E"],
    ["Revenue", "1234", "1456"],
    ["Cost", "800", "900"],
    ["Profit", "434", "556"],
]

BANNER = "Insurance categories summary"


# ---------------- the failures that started this file ----------------


def test_per_glyph_table_is_detected() -> None:
    """Glyph-level runs must not stop the detector from seeing a table."""
    rects = table_rects(TABLE)
    # Sanity: this really is the real-document granularity, not one rect per cell.
    assert len(rects) > sum(len(c) for row in TABLE for c in row) - 5

    grid = detect_grid(rects)

    assert grid is not None
    assert grid.n_cols == 3
    assert grid.n_rows == 4
    assert looks_like_table(grid)
    assert grid.header_rows == 1
    assert grid.cells[0][0] == "Item"
    assert grid.cells[1][1] == "1234"


def test_per_glyph_prose_is_not_a_table() -> None:
    """Wrapped prose with punctuation gaps must not become a grid.

    Fragments vary in width from line to line, exactly as they do when a
    paragraph breaks at punctuation — the shape that produced a fake 3-column
    "table" on a real page.
    """
    lines = [
        (0, [("The market for senior health products keeps", 0), ("growing as the population ages", 520)]),
        (1, [("quickly and household spending shifts", 0), ("toward preventive care", 470)]),
        (2, [("according to the survey we conducted across", 0), ("thirty provinces", 560)]),
        (3, [("with a total of two thousand samples and the", 0), ("results show a split", 590)]),
        (4, [("between urban and rural buyers who differ in", 0), ("awareness and", 540)]),
        (5, [("willingness to pay for services so the channel", 0), ("strategy has to be", 600)]),
        (6, [("rebuilt around the difference between price", 0), ("sensitive shoppers", 500)]),
    ]
    rects: list[TextRect] = []
    for row, fragments in lines:
        top = Y0 - row * ROW_H
        for text, offset in fragments:
            rects.extend(cell_glyphs(text, left=X0 + offset, top=top))

    assert detect_grid(rects) is None


def test_full_width_banner_row_does_not_collapse_columns() -> None:
    """A merged section banner crosses every corridor and must be tolerated.

    This is the "参保险种情况" row from the real certificate: one cell spanning
    the whole table width. Under union projection it merged all columns into one
    and the entire table disappeared.
    """
    rows = [
        ["Item", "2024A", "2025E"],
        [BANNER, "", ""],
        ["Revenue", "1234", "1456"],
        ["Cost", "800", "900"],
        ["Profit", "434", "556"],
    ]

    grid = detect_grid(table_rects(rows))

    assert grid is not None
    assert grid.n_cols == 3
    assert grid.cells[1][0] == BANNER


def test_two_band_header_becomes_two_header_rows() -> None:
    """A merged label whose sub-columns sit on the next band is two header rows.

    Measured on the real certificate: "参保起止时间 | 单位" on one band, then
    "养老 | 工伤 | 失业" on the next. Detecting one header row pushed those
    sub-labels into the data as a stray row of values.
    """
    rows = [
        ["Period", "Company", "", ""],
        ["", "", "Pension", "Medical"],
        ["201409", "Acme", "20", "20"],
        ["201608", "Beta", "34", "34"],
        ["201906", "Gamma", "71", "71"],
    ]
    grid = detect_grid(table_rects(rows))

    assert grid is not None
    assert grid.header_rows == 2
    assert grid.cells[1][2] == "Pension"
    assert grid.cells[2][0] == "201409"


def test_trailing_note_is_left_out_of_the_grid() -> None:
    """A note under the table must not be absorbed into it.

    A real form had a 备注 block right beneath the table; the run swallowed it,
    the numeric ratio fell under the threshold, and the whole table was dropped —
    the second way the certificate returned nothing.
    """
    rows = [
        ["Item", "2024A", "2025E"],
        ["Revenue", "1234", "1456"],
        ["Cost", "800", "900"],
        ["Profit", "434", "556"],
    ]
    rects = table_rects(rows)
    note_lines = [
        "Note the figures above are taken from the audited statements and",
        "have been restated for the change in accounting policy described",
        "in the appendix so they may differ from previously published ones",
    ]
    for i, line in enumerate(note_lines):
        top = Y0 - (len(rows) + i) * ROW_H - 20.0
        rects.extend(cell_glyphs(line, left=X0, top=top))

    grid = detect_grid(rects)

    assert grid is not None
    assert grid.n_rows == 4
    joined = " ".join(c for row in grid.cells for c in row)
    assert "Note" not in joined


def test_narrow_numeric_columns_survive_merged_cells() -> None:
    """The real certificate layout: one wide label column plus narrow numerics.

    A merged banner sits in the middle of the table and the body rows share one
    layout; all four columns still have to come out populated.
    """
    rows = [
        ["Period", "Company", "Pension", "Medical"],
        ["Insurance categories for the period", "", "", ""],
        ["201409", "Acme Data", "20", "20"],
        ["201608", "Beta Info", "34", "34"],
        ["201906", "Gamma Co", "71", "71"],
    ]
    grid = detect_grid(table_rects(rows))

    assert grid is not None
    assert grid.n_cols == 4
    for row in grid.cells[2:]:
        assert row[2].isdigit()
        assert row[3].isdigit()


# ---------------- end to end through the tool ----------------


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_TABLE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VIBE_TRADING_TABLE_ENGINE", "heuristic")
    monkeypatch.delenv("VIBE_TRADING_TABLE_CACHE", raising=False)


def test_tool_keeps_columns_when_a_row_spans_the_page(tmp_path: Path) -> None:
    """End to end: one full-width cell inside a table must not zero the tool."""
    rows = [
        ["Item", "2024A", "2025E"],
        ["Revenue by segment for the period", "", ""],
        ["Revenue", "1234", "1456"],
        ["Cost", "800", "900"],
        ["Profit", "434", "556"],
    ]
    pdf = write_pdf(tmp_path / "banner.pdf", [rows])

    result = json.loads(extract_tables(str(pdf)))

    assert result["status"] == "ok"
    assert result["table_count"] == 1
    table = result["tables"][0]
    assert table["n_cols"] == 3
    assert table["rows"][-1] == ["Profit", "434", "556"]
