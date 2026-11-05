"""Tests for the subprocess isolation wrapper.

The wrapper is exercised against the *heuristic* engine, so these tests need no
docling and no model weights — but they spawn a real child process, which is the
only way to verify that the JSON channel, the UTF-8 pipe and the hard kill
actually work.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from src.tools.tables.engine import get_table_engine
from src.tools.tables.heuristic_engine import HeuristicTableEngine
from src.tools.tables.models import Cell, RawTable
from src.tools.tables.subprocess_engine import SubprocessTableEngine
from tests.test_table_extract_tool import write_pdf

ROWS = [
    ["Item", "2024A", "Share"],
    ["Spirits", "1,234", "45.6%"],
    ["Beer", "567", "21.0%"],
    ["Wine", "89", "3.3%"],
]


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    return write_pdf(tmp_path / "report.pdf", [ROWS])


def test_raw_table_json_round_trip() -> None:
    raw = RawTable(
        page=7,
        bbox=(1.0, 2.0, 3.0, 4.0),
        cells=[[Cell("业务", 2, 1), Cell("1,234")], [Cell("x"), Cell()]],
        header_rows=1,
        caption="表 3：拆分（单位：亿元）",
    )
    restored = RawTable.from_dict(raw.to_dict())

    assert restored.page == 7
    assert restored.bbox == (1.0, 2.0, 3.0, 4.0)
    assert restored.caption == raw.caption
    assert restored.row_texts() == raw.row_texts()
    assert restored.cells[0][0].row_span == 2


def test_round_trip_preserves_grid(pdf: Path) -> None:
    """Same PDF, same engine: isolation must not change the extracted grid."""
    in_process = HeuristicTableEngine().extract(pdf, [1])
    isolated = SubprocessTableEngine(HeuristicTableEngine()).extract(pdf, [1])

    assert [t.page for t in isolated.tables] == [t.page for t in in_process.tables]
    assert [t.row_texts() for t in isolated.tables] == [
        t.row_texts() for t in in_process.tables
    ]
    assert isolated.partial is False


def test_utf8_survives_the_pipe() -> None:
    """Chinese cell text must not be mangled by the Windows console codepage.

    ``write_pdf`` can only emit latin-1, so the pipe is verified with a child
    that prints a hand-built payload instead.
    """
    script = (
        "import json;"
        "print(json.dumps({'ok': True, 'tables': [{'page': 1, 'bbox': [0,0,1,1],"
        " 'cells': [[{'text': '营业收入', 'row_span': 1, 'col_span': 1},"
        " {'text': '1,234', 'row_span': 1, 'col_span': 1}]],"
        " 'header_rows': 1, 'caption': '表 3：拆分（单位：亿元）', 'confidence': None}],"
        " 'pages_read': [1], 'partial': False}, ensure_ascii=False))"
    )

    class Stub(SubprocessTableEngine):
        def command(self) -> list[str]:
            return [sys.executable, "-c", script]

    batch = Stub(HeuristicTableEngine()).extract(Path("x.pdf"), [1])

    assert batch.tables[0].cells[0][0].text == "营业收入"
    assert batch.tables[0].caption == "表 3：拆分（单位：亿元）"


def test_wrapper_forwards_engine_identity() -> None:
    """Cache keys and the envelope's `quality` field depend on these."""
    inner = HeuristicTableEngine()
    wrapper = SubprocessTableEngine(inner)

    assert wrapper.name == inner.name == "heuristic"
    assert wrapper.version == inner.version
    assert wrapper.install_hint == inner.install_hint
    assert wrapper.supports(Path("a.pdf")) is True
    assert wrapper.supports(Path("a.docx")) is False


def test_timeout_kills_the_child() -> None:
    class Slow(SubprocessTableEngine):
        def command(self) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(30)"]

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exceeded"):
        Slow(HeuristicTableEngine()).extract(Path("x.pdf"), [1], deadline=time.monotonic() + 1)
    assert time.monotonic() - started < 15


def test_child_error_is_surfaced() -> None:
    class Broken(SubprocessTableEngine):
        def command(self) -> list[str]:
            return [sys.executable, "-c", "print('{\"ok\": false, \"error\": \"model exploded\"}')"]

    with pytest.raises(RuntimeError, match="model exploded"):
        Broken(HeuristicTableEngine()).extract(Path("x.pdf"), [1])


def test_unparseable_child_output_is_an_error_not_an_empty_result() -> None:
    """Silently returning zero tables would make the model reason about nothing."""

    class Chatty(SubprocessTableEngine):
        def command(self) -> list[str]:
            return [sys.executable, "-c", "print('Segmentation fault')"]

    with pytest.raises(RuntimeError, match="table worker failed"):
        Chatty(HeuristicTableEngine()).extract(Path("x.pdf"), [1])


def test_get_table_engine_wraps_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_TABLE_ENGINE", "heuristic")

    monkeypatch.setenv("VIBE_TRADING_TABLE_SUBPROCESS", "0")
    assert not isinstance(get_table_engine(), SubprocessTableEngine)

    monkeypatch.setenv("VIBE_TRADING_TABLE_SUBPROCESS", "1")
    wrapped = get_table_engine()
    assert isinstance(wrapped, SubprocessTableEngine)
    assert wrapped.name == "heuristic"


def test_worker_refuses_to_spawn_a_grandchild(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child clears the flag; resolve_engine must ignore it entirely."""
    from src.tools.tables.engine import resolve_engine

    monkeypatch.setenv("VIBE_TRADING_TABLE_ENGINE", "heuristic")
    monkeypatch.setenv("VIBE_TRADING_TABLE_SUBPROCESS", "1")

    assert isinstance(resolve_engine(), HeuristicTableEngine)
