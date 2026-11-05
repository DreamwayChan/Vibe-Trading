"""Tests for the docling-based structural engine.

docling is an optional extra, so the interesting logic is deliberately split
out: :func:`build_raw_table` takes duck-typed objects and is exercised here with
fakes, and the converter plumbing is exercised with a stub converter. Nothing in
this file needs docling installed — the one test that requires the real package
is marked ``integration`` and skips itself.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from src.tools.tables import docling_engine
from src.tools.tables.docling_engine import (
    DoclingTableEngine,
    build_raw_table,
    page_runs,
)
from src.tools.tables.engine import TableEngineUnavailable, get_table_engine
from src.tools.tables.normalize import normalize_table


# --------------------------------------------------------------------------
# fakes shaped like docling's document model
# --------------------------------------------------------------------------


class FakeCell:
    def __init__(
        self,
        text: str,
        row: int,
        col: int,
        *,
        row_span: int = 1,
        col_span: int = 1,
        row_header: bool = False,
        column_header: bool = False,
    ) -> None:
        self.text = text
        self.start_row_offset_idx = row
        self.end_row_offset_idx = row + row_span
        self.start_col_offset_idx = col
        self.end_col_offset_idx = col + col_span
        self.row_span = row_span
        self.col_span = col_span
        self.row_header = row_header
        self.column_header = column_header


def fake_table(
    n_rows: int,
    n_cols: int,
    cells: list[FakeCell],
    *,
    page: int = 1,
    bbox: tuple[float, float, float, float] = (10.0, 20.0, 300.0, 500.0),
    caption: str = "",
) -> SimpleNamespace:
    l, b, r, t = bbox
    prov = [SimpleNamespace(page_no=page, bbox=SimpleNamespace(l=l, t=t, r=r, b=b))]
    return SimpleNamespace(
        data=SimpleNamespace(num_rows=n_rows, num_cols=n_cols, table_cells=cells),
        prov=prov,
        captions=[],
        caption_text=lambda doc=None: caption,
    )


def grid_table(rows: list[list[str]], *, page: int = 1, caption: str = "") -> SimpleNamespace:
    """Plain grid, no merges — the common case."""
    cells = [
        FakeCell(text, r, c, column_header=(r == 0))
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
        if text
    ]
    return fake_table(len(rows), max(len(r) for r in rows), cells, page=page, caption=caption)


# --------------------------------------------------------------------------
# page_runs
# --------------------------------------------------------------------------


def test_page_runs_contiguous() -> None:
    assert page_runs([1, 2, 3]) == [(1, 3)]


def test_page_runs_splits_large_gaps() -> None:
    assert page_runs([4, 9, 10]) == [(4, 4), (9, 10)]


def test_page_runs_merges_single_page_gap() -> None:
    # Converting the skipped page costs less than a second pipeline pass.
    assert page_runs([4, 6]) == [(4, 6)]


def test_page_runs_dedupes_and_sorts() -> None:
    assert page_runs([5, 3, 3, 4]) == [(3, 5)]


def test_page_runs_empty() -> None:
    assert page_runs([]) == []


# --------------------------------------------------------------------------
# build_raw_table
# --------------------------------------------------------------------------


def test_build_raw_table_simple_grid() -> None:
    table = grid_table([["业务", "2024A"], ["白酒", "1,234"], ["啤酒", "567"]])
    raw = build_raw_table(table, page=4)

    assert raw is not None
    assert raw.page == 4
    assert (raw.n_rows, raw.n_cols) == (3, 2)
    assert raw.header_rows == 1
    assert raw.row_texts() == [["业务", "2024A"], ["白酒", "1,234"], ["啤酒", "567"]]


def test_build_raw_table_reads_two_row_header_from_column_header_flag() -> None:
    """A two-level header must yield header_rows=2 so flatten_columns can join it."""
    cells = [
        FakeCell("业务", 0, 0, row_span=2, row_header=True, column_header=True),
        FakeCell("2024A", 0, 1, col_span=2, column_header=True),
        FakeCell("收入", 1, 1, column_header=True),
        FakeCell("占比", 1, 2, column_header=True),
        FakeCell("白酒", 2, 0),
        FakeCell("1,234", 2, 1),
        FakeCell("45.6%", 2, 2),
    ]
    raw = build_raw_table(fake_table(3, 3, cells), page=7)

    assert raw is not None
    assert raw.header_rows == 2
    assert raw.row_texts() == [
        ["业务", "2024A", ""],
        ["", "收入", "占比"],
        ["白酒", "1,234", "45.6%"],
    ]


def test_build_raw_table_flattens_merged_header_through_normalize() -> None:
    """End-to-end: the spans above must produce parent/child column names."""
    cells = [
        FakeCell("业务", 0, 0, row_span=2, row_header=True, column_header=True),
        FakeCell("2024A", 0, 1, col_span=2, column_header=True),
        FakeCell("收入", 1, 1, column_header=True),
        FakeCell("占比", 1, 2, column_header=True),
        FakeCell("白酒", 2, 0),
        FakeCell("1,234", 2, 1),
        FakeCell("45.6%", 2, 2),
    ]
    raw = build_raw_table(fake_table(3, 3, cells), page=7)
    assert raw is not None

    record = normalize_table(raw, table_id="t1")
    assert record is not None
    assert record.columns == ["业务", "2024A/收入", "2024A/占比"]
    assert record.rows == [["白酒", "1,234", "45.6%"]]


def test_build_raw_table_repeats_merged_row_header() -> None:
    """A group label spanning three product rows must not leave two blank rows.

    Silently blank row labels would keep the numbers but lose which product they
    belong to — the CSV would be numerically complete and analytically useless.
    """
    cells = [
        FakeCell("产品", 0, 0, column_header=True),
        FakeCell("收入", 0, 1, column_header=True),
        FakeCell("白酒", 1, 0, row_span=3, row_header=True),
        FakeCell("1", 1, 1),
        FakeCell("2", 2, 1),
        FakeCell("3", 3, 1),
        FakeCell("啤酒", 4, 0, row_header=True),
        FakeCell("4", 4, 1),
    ]
    raw = build_raw_table(fake_table(5, 2, cells), page=1)

    assert raw is not None
    assert [r[0] for r in raw.row_texts()] == ["产品", "白酒", "白酒", "白酒", "啤酒"]


def test_build_raw_table_does_not_repeat_wide_data_cells() -> None:
    """A data cell spanning two columns must not duplicate its number."""
    cells = [
        FakeCell("项目", 0, 0, column_header=True),
        FakeCell("值", 0, 1, col_span=2, column_header=True),
        FakeCell("合计", 1, 0, row_header=True),
        FakeCell("100", 1, 1, col_span=2),
    ]
    raw = build_raw_table(fake_table(2, 3, cells), page=1)

    assert raw is not None
    assert raw.row_texts()[1] == ["合计", "100", ""]


def test_build_raw_table_defaults_to_one_header_row_when_unflagged() -> None:
    cells = [FakeCell("业务", 0, 0), FakeCell("收入", 0, 1), FakeCell("白酒", 1, 0), FakeCell("9", 1, 1)]
    raw = build_raw_table(fake_table(2, 2, cells), page=1)

    assert raw is not None
    assert raw.header_rows == 1


def test_build_raw_table_clamps_header_to_leave_a_body_row() -> None:
    """Every row flagged as header would leave nothing to analyse."""
    cells = [
        FakeCell("a", 0, 0, column_header=True),
        FakeCell("b", 1, 0, column_header=True),
    ]
    raw = build_raw_table(fake_table(2, 1, cells), page=1)

    assert raw is not None
    assert raw.header_rows == 1


def test_build_raw_table_handles_out_of_grid_offsets() -> None:
    cells = [FakeCell("a", 0, 0, column_header=True), FakeCell("spill", 9, 9)]
    raw = build_raw_table(fake_table(2, 2, cells), page=1)

    assert raw is not None
    assert raw.n_rows == 2


def test_build_raw_table_returns_none_without_geometry() -> None:
    assert build_raw_table(SimpleNamespace(data=None)) is None
    assert build_raw_table(SimpleNamespace(data=SimpleNamespace(num_rows=0, num_cols=3, table_cells=[]))) is None


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        # BOTTOMLEFT origin (t above b)
        ((10.0, 20.0, 300.0, 500.0), (10.0, 20.0, 300.0, 500.0)),
        # TOPLEFT origin (t below b) must be normalised, not passed through.
        ((10.0, 500.0, 300.0, 20.0), (10.0, 20.0, 300.0, 500.0)),
    ],
)
def test_build_raw_table_normalises_bbox_origin(bbox, expected) -> None:
    table = fake_table(2, 1, [FakeCell("a", 0, 0), FakeCell("b", 1, 0)], bbox=bbox)
    raw = build_raw_table(table, page=1)
    assert raw is not None
    assert raw.bbox == expected


def test_caption_via_caption_text() -> None:
    table = grid_table([["a", "b"], ["1", "2"]], caption="表 3：营业收入拆分（单位：亿元）")
    raw = build_raw_table(table, page=1)
    assert raw is not None
    assert raw.caption == "表 3：营业收入拆分（单位：亿元）"


def test_caption_falls_back_to_resolved_refs() -> None:
    """Older minors only expose ``captions`` refs, not ``caption_text``."""
    table = grid_table([["a", "b"], ["1", "2"]])
    del table.caption_text
    table.captions = [SimpleNamespace(text="表 1：产能"), SimpleNamespace(text="")]
    raw = build_raw_table(table, page=1)

    assert raw is not None
    assert raw.caption == "表 1：产能"


def test_confidence_is_never_invented() -> None:
    raw = build_raw_table(grid_table([["a", "b"], ["1", "2"]]), page=1)
    assert raw is not None
    assert raw.confidence is None


# --------------------------------------------------------------------------
# engine selection
# --------------------------------------------------------------------------


def test_explicit_docling_request_fails_with_install_hint() -> None:
    if DoclingTableEngine().is_available():
        pytest.skip("docling is installed in this environment")
    with pytest.raises(TableEngineUnavailable) as excinfo:
        get_table_engine("docling")
    assert "docling" in str(excinfo.value)


def test_auto_mode_falls_back_to_heuristic_without_docling() -> None:
    if DoclingTableEngine().is_available():
        pytest.skip("docling is installed in this environment")
    engine = get_table_engine("auto")
    assert engine.name == "heuristic"


# --------------------------------------------------------------------------
# extract pipeline (stub converter — no docling, no PDF)
# --------------------------------------------------------------------------


class StubConverter:
    """Stands in for ``DocumentConverter``.

    ``renumber=True`` reproduces the backends that report page numbers relative
    to the requested window instead of absolute document pages.
    """

    def __init__(self, tables_by_page: dict[int, list], *, renumber: bool = False) -> None:
        self.tables_by_page = tables_by_page
        self.renumber = renumber
        self.calls: list[tuple] = []

    def convert(self, path, page_range=None, max_num_pages=None):
        self.calls.append((page_range, max_num_pages))
        start, end = page_range or (1, max_num_pages or 1)
        out = []
        for page, tables in self.tables_by_page.items():
            if not start <= page <= end:
                continue
            for table in tables:
                if self.renumber:
                    table.prov[0].page_no = table.prov[0].page_no - start + 1
                out.append(table)
        return SimpleNamespace(document=SimpleNamespace(tables=out))


def engine_with(converter: StubConverter, *, page_range: bool = True) -> DoclingTableEngine:
    engine = DoclingTableEngine()
    engine._converter = converter
    engine._page_range_supported = page_range
    return engine


def test_extract_converts_only_the_requested_windows() -> None:
    converter = StubConverter(
        {
            4: [grid_table([["a", "b"], ["1", "2"]], page=4)],
            7: [grid_table([["c", "d"], ["3", "4"]], page=7)],
            9: [grid_table([["e", "f"], ["5", "6"]], page=9)],
        }
    )
    engine = engine_with(converter)
    batch = engine.extract("report.pdf", [4, 7])

    assert [t.page for t in batch.tables] == [4, 7]
    assert converter.calls == [((4, 4), None), ((7, 7), None)]


def test_extract_drops_tables_from_pages_outside_the_selection() -> None:
    """A merged window (gap of 1) converts an extra page; its tables must be filtered."""
    converter = StubConverter(
        {
            4: [grid_table([["a", "b"], ["1", "2"]], page=4)],
            5: [grid_table([["x", "y"], ["0", "0"]], page=5)],
            6: [grid_table([["c", "d"], ["3", "4"]], page=6)],
        }
    )
    engine = engine_with(converter)
    batch = engine.extract("report.pdf", [4, 6])

    assert converter.calls == [((4, 6), None)]  # gap merged into one pass
    assert [t.page for t in batch.tables] == [4, 6]


def test_extract_corrects_relative_page_numbers() -> None:
    """Backends that renumber inside a page_range must not shift the page label."""
    converter = StubConverter({5: [grid_table([["a", "b"], ["1", "2"]], page=5)]}, renumber=True)
    engine = engine_with(converter)
    batch = engine.extract("report.pdf", [5])

    assert batch.tables[0].page == 5


def test_extract_whole_document_fallback_when_page_range_unsupported() -> None:
    converter = StubConverter({12: [grid_table([["a", "b"], ["1", "2"]], page=12)]})
    engine = engine_with(converter, page_range=False)
    batch = engine.extract("report.pdf", [12])

    # No page_range: one conversion bounded by max_num_pages, filtered afterwards.
    assert converter.calls == [(None, 12)]
    assert [t.page for t in batch.tables] == [12]


def test_extract_stops_at_max_tables() -> None:
    tables = {i: [grid_table([["a", "b"], ["1", "2"]], page=i)] for i in range(1, 6)}
    engine = engine_with(StubConverter(tables))
    batch = engine.extract("report.pdf", [1, 2, 3, 4, 5], max_tables=2)

    assert len(batch.tables) == 2
    assert batch.partial is True


def test_extract_respects_deadline_without_converting() -> None:
    converter = StubConverter({1: []})
    engine = engine_with(converter)
    batch = engine.extract("report.pdf", [1, 2], deadline=time.monotonic() - 1)

    assert batch.partial is True
    assert converter.calls == []


def test_extract_raises_only_when_every_window_fails() -> None:
    class Broken:
        def convert(self, *args, **kwargs):
            raise RuntimeError("table model exploded")

    engine = engine_with(Broken())
    with pytest.raises(RuntimeError, match="exploded"):
        engine.extract("report.pdf", [1])


def test_extract_tolerates_a_partial_window_failure() -> None:
    """One unreadable page range must not discard tables from the others."""

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def convert(self, path, page_range=None, max_num_pages=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad page")
            return SimpleNamespace(
                document=SimpleNamespace(tables=[grid_table([["a"], ["1"]], page=9)])
            )

    engine = engine_with(Flaky())
    batch = engine.extract("report.pdf", [1, 9])

    assert [t.page for t in batch.tables] == [9]


def test_progress_is_reported_once_per_window() -> None:
    converter = StubConverter({1: [], 5: []})
    engine = engine_with(converter)
    seen: list[tuple[int, int]] = []
    engine.extract("report.pdf", [1, 5], progress=lambda c, t: seen.append((c, t)))

    assert seen == [(1, 2), (2, 2)]


@pytest.mark.integration
def test_real_docling_extraction(tmp_path) -> None:
    """Smoke test against the real package; skipped unless docling is installed."""
    if not DoclingTableEngine().is_available():
        pytest.skip("docling not installed")

    from tests.test_table_extract_tool import write_pdf

    pdf = write_pdf(
        tmp_path / "report.pdf",
        [
            [
                ["业务", "2024A", "占比"],
                ["白酒", "1,234", "45.6%"],
                ["啤酒", "567", "21.0%"],
            ]
        ],
    )
    engine = DoclingTableEngine()
    batch = engine.extract(pdf, [1])

    assert batch.tables, "docling found no table in a synthetic 3x3 grid"
    record = normalize_table(batch.tables[0], table_id="t1")
    assert record is not None
    assert record.n_cols >= 3


def test_module_exposes_engine_contract() -> None:
    engine = DoclingTableEngine()
    assert engine.name == "docling"
    assert "docling" in engine.install_hint
    assert isinstance(engine.version, str)
    assert docling_engine._CONVERT_LOCK is not None
