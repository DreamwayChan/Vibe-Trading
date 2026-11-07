"""Tests for the geometry primitives (pure float tuples, no PDF file).

The regression that matters most here is :func:`test_split_run_in_one_cell_is_merged`:
pypdfium2 returns one rect per text *run*, so a cell mixing scripts arrives as
several rects. Treating each rect as a cell turned a 6-column table into a
7-column one in the measured sample (design doc §16).
"""

from __future__ import annotations

from src.tools.tables.geometry import (
    TextRect,
    band_rows,
    detect_grid,
    is_toc_band,
    looks_like_table,
)

COL_PITCH = 110.0
ROW_PITCH = 14.0
ROW_HEIGHT = 9.0


def rect(left: float, top: float, text: str, *, width: float | None = None) -> TextRect:
    w = width if width is not None else len(text) * 4.6
    return TextRect(left, top - ROW_HEIGHT, left + w, top, text)


def grid_rects(
    rows: list[list[str]],
    *,
    x0: float = 50.0,
    y0: float = 760.0,
    col_pitch: float = COL_PITCH,
    row_pitch: float = ROW_PITCH,
) -> list[TextRect]:
    out: list[TextRect] = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if not text:
                continue
            out.append(rect(x0 + c * col_pitch, y0 - r * row_pitch, text))
    return out


SAMPLE = [
    ["项目", "2023A", "2024E"],
    ["营业收入", "1,234", "1,456"],
    ["净利润", "123", "145"],
    ["毛利率", "12.3%", "14.5%"],
    ["期间费用率", "9.1%", "8.8%"],
]


# ---------------- rows ----------------

def test_band_rows_groups_by_y_and_orders_top_down() -> None:
    rects = grid_rects(SAMPLE)
    bands = band_rows(rects)
    assert len(bands) == len(SAMPLE)
    assert bands[0][0].text == "项目"  # top of the page first
    assert bands[-1][0].text == "期间费用率"


def test_band_rows_tolerates_baseline_jitter() -> None:
    """Cells on one visual row rarely share an exact y."""
    rects = [
        rect(50, 760.0, "a"),
        rect(160, 759.4, "b"),
        rect(270, 760.6, "c"),
        rect(50, 746.0, "d"),
    ]
    bands = band_rows(rects)
    assert len(bands) == 2
    assert len(bands[0]) == 3


# ---------------- grid detection ----------------

def test_detect_grid_recovers_shape() -> None:
    result = detect_grid(grid_rects(SAMPLE))
    assert result is not None
    assert result.n_rows == 5
    assert result.n_cols == 3
    assert result.cells[0] == ["项目", "2023A", "2024E"]
    assert result.cells[1] == ["营业收入", "1,234", "1,456"]


def test_detect_grid_marks_header_row() -> None:
    result = detect_grid(grid_rects(SAMPLE))
    assert result is not None
    assert result.header_rows == 1


def test_detect_grid_no_header_when_first_row_is_data() -> None:
    rows = [
        ["1,234", "5,678", "9.9%"],
        ["1,300", "5,700", "9.8%"],
        ["1,400", "5,800", "9.7%"],
        ["1,500", "5,900", "9.6%"],
    ]
    result = detect_grid(grid_rects(rows))
    assert result is not None
    assert result.header_rows == 0


def test_detect_grid_needs_three_rows() -> None:
    rows = [["a", "1", "2"], ["b", "3", "4"]]
    assert detect_grid(grid_rects(rows)) is None


def test_split_run_in_one_cell_is_merged() -> None:
    """Regression: one cell rendered as two text runs must not become two columns.

    In the measured sample the fragments sat 1.1 pt apart inside a cell while
    columns were 48 pt apart; treating each rect as a cell produced 7 columns
    for a 6-column table.
    """
    rects = grid_rects(SAMPLE)
    # Replace the "营业收入" rect with two fragments 1.1 pt apart.
    rects = [r for r in rects if r.text != "营业收入"]
    rects.append(rect(50.0, 760.0 - ROW_PITCH, "营业", width=12.0))
    rects.append(rect(63.1, 760.0 - ROW_PITCH, "收入", width=12.0))

    result = detect_grid(rects)
    assert result is not None
    assert result.n_cols == 3, "cell fragments must merge into one column"
    assert result.cells[1][0] == "营业收入"


def test_sparse_aligned_column_is_purged() -> None:
    """A value aligned with the grid but present in one row only is not a column."""
    rects = grid_rects(SAMPLE)
    rects.append(rect(50.0 + 3 * COL_PITCH, 760.0, "页眉"))  # only in row 0

    result = detect_grid(rects)
    assert result is not None
    assert result.n_cols == 3


# ---------------- data-vs-prose gate ----------------

def test_prose_band_is_rejected_by_numeric_ratio() -> None:
    """Three aligned words are structurally grid-like but carry no data."""
    rows = [[f"文字{i}", f"说明{i}", f"备注{i}"] for i in range(5)]
    result = detect_grid(grid_rects(rows))
    if result is not None:
        assert looks_like_table(result) is False


def test_numeric_table_is_accepted() -> None:
    assert looks_like_table(detect_grid(grid_rects(SAMPLE))) is True


# ---------------- misc ----------------

def test_caption_is_taken_from_the_band_above() -> None:
    rects = grid_rects(SAMPLE, y0=740.0)
    rects.extend([rect(50.0, 770.0, "表"), rect(70.0, 770.0, "1：主要财务指标")])

    result = detect_grid(rects)
    assert result is not None
    assert "主要财务指标" in result.caption


def test_caption_ignored_when_far_above() -> None:
    rects = grid_rects(SAMPLE, y0=700.0)
    rects.extend([rect(50.0, 790.0, "上一节正文文字")])

    result = detect_grid(rects)
    assert result is not None
    assert result.caption == ""


def test_is_toc_band() -> None:
    toc = [rect(50.0, 700.0, "第一章 行业概况............12")]
    body = [rect(50.0, 700.0, "公司 2024 年营业收入同比增长 12.3%")]
    assert is_toc_band(toc) is True
    assert is_toc_band(body) is False


def test_bbox_covers_all_cells() -> None:
    result = detect_grid(grid_rects(SAMPLE))
    assert result is not None
    left, bottom, right, top = result.bbox
    assert left == 50.0
    assert right > 50.0 + 2 * COL_PITCH
    assert top <= 760.0
