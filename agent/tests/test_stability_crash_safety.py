"""§8.2 稳定性：写一半被杀、原子写失败、残留临时文件、空快照覆盖。

纸面账本是这套系统里唯一"错了就亏钱"的产物，所以它对崩溃的要求不是"能恢复"，
而是"失败时留旧值、绝不留半截"。这里从三个角度钉住这一点：

* 原子替换失败 → 旧文件仍可读，临时文件不残留；
* 进程被 kill（模拟断电/重启）→ 盘上每个 json 都能解析，没有撕裂文件；
* 空表写入 / 重复写入 → 不抹掉已有分区、不产生重复行（2026-11-05 空快照事故）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import src.paper_book.store as store
from src.data import lake
from src.paper_book.models import BookState, DailyReport
from src.paper_book.store import (
    PaperBookError,
    list_report_dates,
    load_report,
    load_state,
    save_report,
    save_state,
)

BOOK = "crash-book"


def _state(cash: float) -> BookState:
    return BookState(book_id=BOOK, cash=cash)


def _report(as_of: str, nav: float) -> DailyReport:
    return DailyReport(
        book_id=BOOK,
        run_id="run-1",
        as_of=as_of,
        nav=nav,
        cash=nav,
        market_value=0.0,
        initial_cash=1_000_000.0,
        prev_nav=nav,
        daily_return=0.0,
        cum_return=0.0,
        turnover=0.0,
    )


# --------------------------------------------------------------------------
# 原子写：失败时不能毁掉上一版
# --------------------------------------------------------------------------


def test_a_failed_replace_keeps_the_previous_state_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk full / permission denied at the last step must not lose the book."""
    save_state(tmp_path, _state(900_000.0))
    path = store.book_dir(tmp_path, BOOK) / "state.json"
    previous = path.read_bytes()

    def _boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store.os, "replace", _boom)

    with pytest.raises(OSError):
        save_state(tmp_path, _state(100.0))

    assert path.read_bytes() == previous
    restored = load_state(tmp_path, BOOK)
    assert restored is not None and restored.cash == 900_000.0
    # The half-written temp file must not be left for the next reader to trip on.
    assert [p.name for p in path.parent.iterdir() if p.name != "state.json"] == []


def test_a_leftover_tmp_file_is_not_mistaken_for_a_report(tmp_path: Path) -> None:
    """A kill between write and replace leaves ``<date>.json.tmp`` behind."""
    save_report(tmp_path, _report("2026-01-05", 1_000_000.0))
    reports = store.book_dir(tmp_path, BOOK) / "reports"
    (reports / "2026-01-06.json.tmp").write_text('{"nav": 12', encoding="utf-8")

    assert list_report_dates(tmp_path, BOOK) == ["2026-01-05"]
    assert load_report(tmp_path, BOOK, "2026-01-06") is None


def test_a_truncated_report_is_surfaced_not_silently_ignored(tmp_path: Path) -> None:
    """Returning None here would read as "that day never settled" and quietly
    re-run the session from the wrong NAV."""
    save_report(tmp_path, _report("2026-01-05", 1_000_000.0))
    path = store.book_dir(tmp_path, BOOK) / "reports" / "2026-01-05.json"
    path.write_text(path.read_text(encoding="utf-8")[: len(path.read_text()) // 2], encoding="utf-8")

    with pytest.raises(PaperBookError):
        load_report(tmp_path, BOOK, "2026-01-05")


# --------------------------------------------------------------------------
# 真实 kill：子进程一边写一边被杀，盘上不留撕裂文件
# --------------------------------------------------------------------------

_CHILD = '''
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from src.paper_book.models import DailyReport
from src.paper_book.store import save_report

root = Path(sys.argv[1])
day = date(2026, 1, 1)
while True:
    save_report(
        root,
        DailyReport(
            book_id="crash-book", run_id="run-1", as_of=day.isoformat(),
            nav=1_000_000.0, cash=1_000_000.0, market_value=0.0,
            initial_cash=1_000_000.0, prev_nav=1_000_000.0,
            daily_return=0.0, cum_return=0.0, turnover=0.0,
        ),
    )
    day += timedelta(days=1)
'''


def test_killing_the_writer_mid_flight_leaves_only_parseable_reports(tmp_path: Path) -> None:
    """SIGKILL has no chance to clean up: whatever is on disk must still load."""
    script = tmp_path / "writer.py"
    script.write_text(_CHILD, encoding="utf-8")
    agent_dir = str(Path(__file__).resolve().parent.parent)
    root = tmp_path / "book-root"

    child = subprocess.Popen(
        [sys.executable, str(script), str(root), agent_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        reports = root / BOOK / "reports"
        deadline = time.time() + 20
        while time.time() < deadline:
            if reports.is_dir() and len(list(reports.glob("*.json"))) >= 5:
                break
            time.sleep(0.02)
        else:  # pragma: no cover - only on a pathologically slow machine
            pytest.fail("child never wrote five reports")
    finally:
        child.kill()
        child.wait(timeout=10)

    written = list_report_dates(root, BOOK)
    assert len(written) >= 5
    for day in written:
        payload = load_report(root, BOOK, day)
        assert isinstance(payload, dict) and payload["nav"] == 1_000_000.0
    # Leftover temp files are allowed (the process was killed), but they must
    # never be counted as settled sessions.
    assert all(not day.endswith(".tmp") for day in written)


# --------------------------------------------------------------------------
# 空快照 / 重复写入：数据湖不能被失败的抓取抹平
# --------------------------------------------------------------------------


def _bars(rows: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2026-01-05", periods=rows),
            "code": ["600519.SH"] * rows,
            "close": [100.0 + index for index in range(rows)],
        }
    )


def test_an_empty_fetch_never_erases_a_stored_partition(tmp_path: Path, monkeypatch) -> None:
    """The 2026-11-05 accident: a failed request wrote an empty snapshot over
    good bars, and every downstream report then said "no data"."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path / "lake"))
    from src.config.accessor import reset_env_config

    reset_env_config()

    lake.write_dataset(_bars(), "bars_daily", key=["date", "code"], partition_by="date")
    stored = lake.read_dataset("bars_daily")
    assert len(stored) == 3

    assert lake.write_dataset(pd.DataFrame(), "bars_daily", key=["date", "code"]) == 0
    assert lake.write_dataset(None, "bars_daily", key=["date", "code"]) == 0

    assert len(lake.read_dataset("bars_daily")) == 3


def test_reingesting_the_same_rows_does_not_duplicate_them(tmp_path: Path, monkeypatch) -> None:
    """A retry after a timeout must be a row replace, not an append."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path / "lake"))
    from src.config.accessor import reset_env_config

    reset_env_config()

    frame = _bars()
    lake.write_dataset(frame, "bars_daily", key=["date", "code"], partition_by="date")
    lake.write_dataset(frame, "bars_daily", key=["date", "code"], partition_by="date")

    stored = lake.read_dataset("bars_daily")
    assert len(stored) == len(frame)
    assert not stored.duplicated(subset=["date", "code"]).any()
