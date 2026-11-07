"""Tests for cross-page table stitching.

Two layers, deliberately separated: the merge rules are pure functions over
:class:`RawTable` (no PDF needed, one case per rule), and one end-to-end test
proves the pass is actually wired into ``extract_tables`` before normalisation.
"""

from __future__ import annotations

import json

import pytest

from src.tools.tables.continuation import merge_continuations
from src.tools.tables.models import Cell, RawTable
from src.tools.tables.service import extract_tables
from tests.test_table_extract_tool import write_pdf


def table(
    page: int,
    rows: list[list[str]],
    *,
    header_rows: int = 1,
    caption: str = "",
) -> RawTable:
    return RawTable(
        page=page,
        bbox=(50.0, 100.0, 545.0, 700.0),
        cells=[[Cell(text=c) for c in row] for row in rows],
        header_rows=header_rows,
        caption=caption,
    )


HEADER = ["Item", "2024A", "2025E"]


# ---------------- case A: repeated header ----------------


def test_repeated_header_merges_and_drops_duplicate() -> None:
    first = table(4, [HEADER, ["Revenue", "1,234", "1,456"], ["Cost", "-800", "-900"]])
    second = table(5, [HEADER, ["Opex", "-120", "-130"]])

    merged, report = merge_continuations([first, second])

    assert len(merged) == 1
    assert merged[0].pages == [4, 5]
    assert merged[0].page == 4
    # The duplicate header row is gone; only the three data rows remain.
    assert merged[0].row_texts() == [
        HEADER,
        ["Revenue", "1,234", "1,456"],
        ["Cost", "-800", "-900"],
        ["Opex", "-120", "-130"],
    ]
    assert merged[0].header_rows == 1
    assert report.joins == 1
    assert report.chains == [[4, 5]]
    assert report.stitched is True


def test_partial_header_repeat_merges() -> None:
    """A two-row header that re-renders only its leaf row still counts."""
    first = table(
        7,
        [
            ["Project", "2024A", ""],
            ["", "Revenue", "Share"],
            ["Spirits", "1,234", "45.6%"],
        ],
        header_rows=2,
    )
    second = table(
        8,
        [
            ["", "Revenue", "Share"],
            ["Beer", "567", "21.0%"],
        ],
        header_rows=1,
    )

    merged, report = merge_continuations([first, second])

    assert len(merged) == 1
    assert report.joins == 1
    # The repeated leaf row is dropped, the real data row survives.
    assert merged[0].row_texts()[-1] == ["Beer", "567", "21.0%"]
    assert len(merged[0].cells) == 4


def test_whitespace_differences_still_match() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [[" Item ", "2024A", "2025E"], ["Cost", "-800", "-900"]])

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 1
    assert merged[0].row_texts()[-1] == ["Cost", "-800", "-900"]


# ---------------- case B: continuation caption ----------------


def test_continuation_caption_merges_without_header() -> None:
    first = table(4, [HEADER, ["Revenue", "1,234", "1,456"]])
    # Header not repeated: the engine reports header_rows=0 because row 0 is
    # already data (a label plus numbers).
    second = table(
        5,
        [["Opex", "-120", "-130"], ["Net profit", "314", "426"]],
        header_rows=0,
        caption="表 3（续）",
    )

    merged, report = merge_continuations([first, second])

    assert len(merged) == 1
    assert report.joins == 1
    # Nothing was dropped: dropping the first row here would eat a data row.
    assert merged[0].row_texts()[1] == ["Revenue", "1,234", "1,456"]
    assert merged[0].row_texts()[2] == ["Opex", "-120", "-130"]
    assert merged[0].caption == ""  # the chain keeps the FIRST caption


@pytest.mark.parametrize("caption", ["续表 3", "Table 3 (continued)", "接上页"])
def test_caption_markers_are_recognised(caption: str) -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [["Opex", "-120", "-130"]], header_rows=0, caption=caption)

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 1


# ---------------- refusals ----------------


def test_non_adjacent_pages_are_not_merged() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(3, [HEADER, ["Opex", "-120", "-130"]])

    merged, report = merge_continuations([first, second])

    assert len(merged) == 2
    assert report.joins == 0


def test_column_count_mismatch_is_not_merged() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [["Item", "2024A"], ["Opex", "-120"]])

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 2


def test_different_header_is_not_merged() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [["Capacity", "2024A", "2025E"], ["Wafer", "10", "12"]])

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 2


def test_headerless_block_without_caption_is_not_merged() -> None:
    """No repeated header and no marker: could be any table, so stay out."""
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [["Opex", "-120", "-130"]], header_rows=0)

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 2


def test_word_containing_xu_does_not_count_as_continuation() -> None:
    """'持续经营' must not be read as the '（续）' marker."""
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(
        2,
        [["Opex", "-120", "-130"]],
        header_rows=0,
        caption="关于持续经营能力的专项说明",
    )

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 2


def test_single_column_blocks_are_not_merged() -> None:
    first = table(1, [["Item"], ["Revenue"]])
    second = table(2, [["Item"], ["Cost"]])

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 2


def test_two_column_tables_merge_by_default() -> None:
    """项目 | 金额 is a real table shape; the repeated header is the gate."""
    first = table(1, [["Item", "Amount"], ["Revenue", "1,234"]])
    second = table(2, [["Item", "Amount"], ["Cost", "-800"]])

    merged, _ = merge_continuations([first, second])

    assert len(merged) == 1
    assert merged[0].pages == [1, 2]


# ---------------- chains and shapes ----------------


def test_three_page_chain_merges_into_one() -> None:
    tables = [
        table(9, [HEADER, ["A", "1", "2"]]),
        table(10, [HEADER, ["B", "3", "4"]]),
        table(11, [HEADER, ["C", "5", "6"]]),
    ]

    merged, report = merge_continuations(tables)

    assert len(merged) == 1
    assert merged[0].pages == [9, 10, 11]
    assert report.joins == 2
    assert report.chains == [[9, 10, 11]]
    assert [r[0] for r in merged[0].row_texts()[1:]] == ["A", "B", "C"]


def test_ragged_row_is_padded_to_width() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [HEADER, ["Opex", "-120"]])  # trailing column not drawn

    merged, _ = merge_continuations([first, second])

    assert merged[0].n_cols == 3
    assert merged[0].row_texts()[-1] == ["Opex", "-120", ""]


def test_empty_input() -> None:
    merged, report = merge_continuations([])

    assert merged == []
    assert report.joins == 0
    assert report.stitched is False


def test_input_tables_are_not_mutated() -> None:
    first = table(1, [HEADER, ["Revenue", "1,234", "1,456"]])
    second = table(2, [HEADER, ["Opex", "-120", "-130"]])

    merge_continuations([first, second])

    assert first.pages == [1]
    assert len(first.cells) == 2


# ---------------- end to end ----------------


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIBE_TRADING_TABLE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VIBE_TRADING_TABLE_ENGINE", "heuristic")
    monkeypatch.delenv("VIBE_TRADING_TABLE_CACHE", raising=False)


def test_two_page_table_is_stitched_end_to_end(tmp_path) -> None:
    page_one = [
        ["Item", "2024A", "2025E"],
        ["Revenue", "1,234", "1,456"],
        ["Cost", "-800", "-900"],
        ["Gross profit", "434", "556"],
    ]
    page_two = [
        ["Item", "2024A", "2025E"],
        ["Opex", "-120", "-130"],
        ["Net profit", "314", "426"],
    ]
    pdf = write_pdf(tmp_path / "spread.pdf", [page_one, page_two])

    result = json.loads(extract_tables(str(pdf)))

    assert result["status"] == "ok"
    assert result["table_count"] == 1
    record = result["tables"][0]
    assert record["pages"] == [1, 2]
    assert record["page"] == 1
    assert record["columns"] == ["Item", "2024A", "2025E"]
    # 3 + 2 data rows, the second header row collapsed into one.
    assert record["n_rows"] == 6
    assert record["rows"][-1] == ["Net profit", "314", "426"]
    assert result["stitched_pages"] == [[1, 2]]


def test_separate_tables_stay_separate_end_to_end(tmp_path) -> None:
    page_one = [
        ["Item", "2024A", "2025E"],
        ["Revenue", "1,234", "1,456"],
        ["Cost", "-800", "-900"],
        ["Gross profit", "434", "556"],
    ]
    page_two = [
        ["Capacity", "2024A", "2025E"],
        ["Wafer", "10", "12"],
        ["Utilisation", "88%", "91%"],
    ]
    pdf = write_pdf(tmp_path / "two.pdf", [page_one, page_two])

    result = json.loads(extract_tables(str(pdf)))

    assert result["table_count"] == 2
    assert [t["pages"] for t in result["tables"]] == [[1], [2]]
    assert "stitched_pages" not in result
