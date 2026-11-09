"""§8.2 长跑采样的判定逻辑。

``soak_watch.py`` 采样两天没人看就等于没跑。这里锁住 ``--analyze`` 的判据：
一个稳定的进程必须判 pass，任何一种泄漏都必须判 fail，且结论要指名是哪一项。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "soak_watch.py"


@pytest.fixture(scope="module")
def soak():
    spec = importlib.util.spec_from_file_location("soak_watch_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    header = ["ts", "rss_mb", "threads", "handles", "health_ms", "monitor_ms", "ok"]
    path = tmp_path / "soak.csv"
    lines = [",".join(header)]
    for index, row in enumerate(rows):
        lines.append(
            ",".join(
                [
                    f"2026-11-0{1 + index // 24}T{index % 24:02d}:00:00+00:00",
                    str(row.get("rss_mb", 200.0)),
                    str(row.get("threads", 12)),
                    str(row.get("handles", 260)),
                    str(row.get("health_ms", 5.0)),
                    str(row.get("monitor_ms", 9.0)),
                    str(row.get("ok", 1)),
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_flat_process_passes_and_reports_its_window(soak, tmp_path: Path) -> None:
    path = _write(tmp_path, [{"rss_mb": 200.0 + index * 0.01} for index in range(40)])

    report = soak.analyze(path)

    assert report["verdict"] == "pass"
    assert report["findings"] == []
    assert report["samples"] == 40
    assert report["probe_failures"] == 0
    assert report["rss_growth_pct"] == pytest.approx(0.14, abs=0.05)


def test_memory_drift_is_reported_as_a_finding(soak, tmp_path: Path) -> None:
    """300 MB → 600 MB over the window is a leak, not a warm-up."""
    path = _write(tmp_path, [{"rss_mb": 300.0 + index * 8.0} for index in range(40)])

    report = soak.analyze(path)

    assert report["verdict"] == "fail"
    assert any("RSS grew" in finding for finding in report["findings"])
    assert report["rss_first_quarter_mb"] < report["rss_last_quarter_mb"]


def test_handle_growth_is_caught_even_when_memory_is_flat(soak, tmp_path: Path) -> None:
    """A fd/handle leak barely moves RSS, so it needs its own signal."""
    path = _write(tmp_path, [{"handles": 260 + index * 3} for index in range(40)])

    report = soak.analyze(path)

    assert report["verdict"] == "fail"
    assert report["handle_delta"] == pytest.approx(117)
    assert any("handle/fd growth" in finding for finding in report["findings"])


def test_a_single_failed_probe_fails_the_soak(soak, tmp_path: Path) -> None:
    rows = [{} for _ in range(20)]
    rows[7]["ok"] = 0
    rows[7]["health_ms"] = -1.0
    path = _write(tmp_path, rows)

    report = soak.analyze(path)

    assert report["verdict"] == "fail"
    assert report["probe_failures"] == 1
    assert report["findings"][0] == "1 probe failure(s)"


def test_slow_liveness_is_a_finding(soak, tmp_path: Path) -> None:
    path = _write(tmp_path, [{"health_ms": 900.0} for _ in range(20)])

    report = soak.analyze(path)

    assert report["verdict"] == "fail"
    assert report["health_p95_ms"] == pytest.approx(900.0)
    assert any("/health p95" in finding for finding in report["findings"])


def test_an_empty_csv_is_not_silently_a_pass(soak, tmp_path: Path) -> None:
    """Zero samples means the sampler never ran — that must never read as green."""
    path = tmp_path / "soak.csv"
    path.write_text("ts,rss_mb,threads,handles,health_ms,monitor_ms,ok\n", encoding="utf-8")

    report = soak.analyze(path)

    assert report["samples"] == 0
    assert report["verdict"] == "empty"


def test_analyze_mode_exits_nonzero_on_drift(soak, tmp_path: Path, capsys) -> None:
    """CI gates on the exit code, so the CLI contract matters more than the dict."""
    leaking = _write(tmp_path, [{"rss_mb": 300.0 + index * 8.0} for index in range(40)])
    assert soak.main(["--analyze", str(leaking)]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"

    flat_dir = tmp_path / "flat"
    flat_dir.mkdir()
    flat = _write(flat_dir, [{"rss_mb": 300.0} for _ in range(40)])
    assert soak.main(["--analyze", str(flat)]) == 0


def test_sampling_without_a_pid_is_refused(soak, tmp_path: Path) -> None:
    """Guessing a pid would sample some other process and quietly pass."""
    with pytest.raises(SystemExit):
        soak.main(["--out", str(tmp_path / "x.csv")])


def test_posix_sample_reads_this_process_and_misses_a_dead_pid(soak) -> None:
    import os

    rss, threads, handles = soak.sample_process(os.getpid())
    assert rss is not None and rss > 0
    assert threads is not None and threads >= 1
    assert handles is not None and handles >= 1
    assert soak.sample_process(2**30) == (None, None, None)


def test_probe_reports_success_and_failure(soak, monkeypatch) -> None:
    class Response:
        status = 200

        def read(self, _n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(soak.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    elapsed, ok = soak._probe("http://127.0.0.1/health")
    assert ok is True
    assert elapsed is not None and elapsed >= 0

    def boom(*args, **kwargs):
        raise soak.urllib.error.URLError("down")

    monkeypatch.setattr(soak.urllib.request, "urlopen", boom)
    assert soak._probe("http://127.0.0.1/health") == (None, False)


def test_one_sample_writes_a_row_and_stops_on_interrupt(soak, tmp_path, monkeypatch) -> None:
    import os

    def sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(soak.time, "sleep", sleep)
    monkeypatch.setattr(soak, "_probe", lambda url, timeout=5.0: (12.0, False))
    out = tmp_path / "soak.csv"
    code = soak.main(
        ["--pid", str(os.getpid()), "--url", "http://127.0.0.1:9", "--hours", "1", "--interval", "1", "--out", str(out)]
    )
    assert code == 0
    rows = list(soak.csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["ok"] == "0"
    assert rows[0]["health_ms"] == "-1.0"

    # A second run appends and does not write the header again.
    monkeypatch.setattr(soak, "_probe", lambda url, timeout=5.0: (4.0, True))
    assert soak.main(["--pid", str(os.getpid()), "--hours", "1", "--interval", "1", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.count("rss_mb") == 1
    assert len(list(soak.csv.DictReader(out.open(encoding="utf-8")))) == 2


def test_thread_growth_and_blank_cells(soak, tmp_path) -> None:
    path = _write(tmp_path, [{"threads": 1 + index} for index in range(20)])
    report = soak.analyze(path)
    assert report["verdict"] == "fail"
    assert any("thread growth" in finding for finding in report["findings"])

    blank = tmp_path / "blank.csv"
    blank.write_text(
        "ts,rss_mb,threads,handles,health_ms,monitor_ms,ok\n"
        "2026-11-01T00:00:00+00:00,n/a,,,,,\n"
        "2026-11-01T01:00:00+00:00,,,,,,\n",
        encoding="utf-8",
    )
    quiet = soak.analyze(blank)
    assert quiet["verdict"] == "pass"
    assert quiet["rss_growth_pct"] is None
    assert quiet["handle_delta"] is None
    assert quiet["thread_delta"] is None
    assert quiet["health_p95_ms"] is None
    assert quiet["monitor_p95_ms"] is None


def test_the_header_order_is_stable(soak) -> None:
    """The runbook greps these columns by name; reordering breaks it silently."""
    assert soak.HEADER == ["ts", "rss_mb", "threads", "handles", "health_ms", "monitor_ms", "ok"]
