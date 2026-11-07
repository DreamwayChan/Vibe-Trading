"""Tests for finance-aware table normalisation (pure functions, no PDF)."""

from __future__ import annotations

import pytest

from src.tools.tables.models import Cell, RawTable
from src.tools.tables.normalize import (
    clean_cell,
    detect_statement,
    detect_unit,
    flatten_columns,
    infer_column_type,
    is_noise_row,
    normalize_table,
    parse_number,
    render_csv,
    render_markdown,
)


# ---------------- parse_number ----------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234", 1234.0),
        ("1,234,567.89", 1234567.89),
        ("(1,234.5)", -1234.5),          # accountant negative
        ("（1,234.5）", -1234.5),        # full-width parens
        ("−5.2", -5.2),                  # unicode minus
        ("12.3%", pytest.approx(0.123)),
        ("1.2亿", pytest.approx(1.2e8)),
        ("3.5万元", pytest.approx(3.5e4)),
        ("1,200元", 1200.0),
        ("约 1,200", 1200.0),
        ("+8.1%", pytest.approx(0.081)),
        ("0", 0.0),
    ],
)
def test_parse_number_accepts_finance_formats(raw: str, expected: float) -> None:
    assert parse_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "--", "—", "—", "不适用", "n/a", "/", "abc", "2023A"])
def test_parse_number_rejects_non_numbers(raw: str) -> None:
    assert parse_number(raw) is None


def test_parse_number_percent_is_fraction() -> None:
    """Percent must become a fraction, otherwise ratios get compared to 100."""
    assert parse_number("12.3%") == pytest.approx(0.123)


# ---------------- units ----------------

def test_detect_unit_from_caption() -> None:
    label, scale = detect_unit("表 3：营业收入拆分（单位：亿元）", [["a", "1"]])
    assert label == "亿元"
    assert scale == 1e8


def test_detect_unit_from_first_row() -> None:
    label, scale = detect_unit("", [["单位：万元", "", ""], ["a", "1", "2"]])
    assert label == "万元"
    assert scale == 1e4


def test_detect_unit_absent() -> None:
    assert detect_unit("表 1：产能情况", [["a", "1"]]) == (None, None)


def test_detect_unit_prefers_longest_token() -> None:
    """"百万元" must not be read as "万元"."""
    label, scale = detect_unit("单位：百万元", [])
    assert (label, scale) == ("百万元", 1e6)


# ---------------- headers ----------------

def test_flatten_single_row_header() -> None:
    assert flatten_columns([["项目", "2023A"]]) == ["项目", "2023A"]


def test_flatten_multi_row_header_joins_with_slash() -> None:
    header = [
        ["", "2023A", "", "2024E", ""],
        ["项目", "收入", "占比", "收入", "占比"],
    ]
    assert flatten_columns(header) == ["项目", "2023A/收入", "2023A/占比", "2024E/收入", "2024E/占比"]


def test_flatten_dedupes_repeated_merged_cell_values() -> None:
    """A merged header cell repeated across columns collapses to one label."""
    header = [["2023A", "2023A", "2024E"], ["收入", "占比", "收入"]]
    assert flatten_columns(header) == ["2023A/收入", "2023A/占比", "2024E/收入"]


def test_flatten_falls_back_to_positional_names() -> None:
    assert flatten_columns([["", ""]]) == ["col1", "col2"]


# ---------------- column typing ----------------

def test_column_type_number() -> None:
    assert infer_column_type(["1,234", "1,456", "1,678", "n/a"]) == "number"


def test_column_type_date() -> None:
    assert infer_column_type(["2024-01-05", "2024-02-05", "2024-03-05"]) == "date"


def test_column_type_empty_and_text() -> None:
    assert infer_column_type(["", "-", "—"]) == "empty"
    assert infer_column_type(["白酒", "啤酒", "调味品"]) == "text"


def test_column_type_needs_enough_numeric_samples() -> None:
    """Two numbers among many texts must not flip the column to numeric."""
    assert infer_column_type(["1,234", "1,456", "白酒", "啤酒", "调味品"]) == "text"


# ---------------- statement classification ----------------

@pytest.mark.parametrize(
    ("columns", "first_col", "expected"),
    [
        (["营业收入", "营业成本", "净利润"], ["白酒", "啤酒"], "income"),
        (["总资产", "总负债"], ["2023", "2024"], "balance"),
        (["经营活动现金流", "投资活动现金流"], ["2023", "2024"], "cashflow"),
        (["产能", "产量", "价格"], ["一厂", "二厂"], None),
    ],
)
def test_detect_statement(columns: list[str], first_col: list[str], expected: str | None) -> None:
    assert detect_statement(columns, first_col) == expected


# ---------------- noise rows ----------------

@pytest.mark.parametrize(
    "row",
    [
        ["", "", ""],
        ["资料来源：Wind", "", ""],
        ["注：剔除非经常性损益", "", ""],
        ["12", "", ""],
    ],
)
def test_noise_rows_are_dropped(row: list[str]) -> None:
    assert is_noise_row(row) is True


def test_data_row_is_not_noise() -> None:
    assert is_noise_row(["白酒", "1,234", "12.3%"]) is False


# ---------------- rendering ----------------

def test_clean_cell_collapses_whitespace() -> None:
    assert clean_cell("  营业  收入\n合计 ") == "营业 收入 合计"


def test_markdown_escapes_pipe() -> None:
    md, truncated = render_markdown(["a|b"], [["1|2"]], 1000)
    assert "a\\|b" in md
    assert "1\\|2" in md
    assert truncated is False


def test_markdown_truncates_on_budget() -> None:
    rows = [[f"row{i}", str(i)] for i in range(200)]
    md, truncated = render_markdown(["名称", "值"], rows, 200)
    assert truncated is True
    assert len(md) <= 260  # budget plus the two header lines


def test_csv_quotes_commas() -> None:
    csv_text, _ = render_csv(["名称"], [["1,234"]], 1000)
    assert '"1,234"' in csv_text


# ---------------- end-to-end normalisation ----------------

def _raw(caption: str = "", header: int = 1) -> RawTable:
    grid = [
        ["项目", "2023A", "2024E"],
        ["营业收入", "1,234", "1,456"],
        ["净利润", "123", "145"],
        ["毛利率", "12.3%", "14.5%"],
    ]
    return RawTable(
        page=3,
        bbox=(0.0, 0.0, 100.0, 100.0),
        cells=[[Cell(text=c) for c in row] for row in grid],
        header_rows=header,
        caption=caption,
    )


def test_normalize_table_produces_record() -> None:
    record = normalize_table(_raw("表 1：主要财务指标（单位：亿元）"), table_id="t1")
    assert record is not None
    assert record.id == "t1"
    assert record.page == 3
    assert record.columns == ["项目", "2023A", "2024E"]
    assert record.column_types == ["text", "number", "number"]
    assert record.unit_label == "亿元"
    assert record.unit_scale == 1e8
    assert record.statement_hint == "income"
    assert record.n_rows == 4
    assert "| 营业收入 |" in record.markdown


def test_normalize_table_without_header_pads_positional_names() -> None:
    record = normalize_table(_raw(header=0), table_id="t1")
    assert record is not None
    assert record.columns[0].startswith("col")
    # Every original row survives, since none of them was a header.
    assert record.n_rows == 4


def test_normalize_table_drops_unit_row() -> None:
    grid = RawTable(
        page=1,
        bbox=(0, 0, 1, 1),
        cells=[
            [Cell(text="项目"), Cell(text="2023A")],
            [Cell(text="单位：亿元"), Cell(text="")],
            [Cell(text="营业收入"), Cell(text="1,234")],
        ],
        header_rows=1,
    )
    record = normalize_table(grid, table_id="t1")
    assert record is not None
    assert all("单位" not in c for row in record.rows for c in row)
    assert record.unit_label == "亿元"


def test_normalize_table_returns_none_when_empty() -> None:
    empty = RawTable(page=1, bbox=(0, 0, 0, 0), cells=[], header_rows=1)
    assert normalize_table(empty, table_id="t1") is None


def test_normalize_table_respects_char_budget() -> None:
    grid = [[Cell(text=f"c{r}-{c}") for c in range(4)] for r in range(120)]
    raw = RawTable(page=1, bbox=(0, 0, 1, 1), cells=grid, header_rows=1)
    record = normalize_table(raw, table_id="t1", max_chars=500)
    assert record is not None
    assert record.truncated is True
    assert len(record.markdown) <= 600
