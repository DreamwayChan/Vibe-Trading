"""Long-run sampler for the 72h soak (§8.2).

Samples the API process every ``--interval`` seconds and appends one CSV row:

    ts, rss_mb, threads, handles, health_ms, monitor_ms, ok

* ``rss_mb`` / ``threads`` / ``handles`` come from the OS (``psapi`` on Windows,
  ``/proc`` on POSIX) — no third-party dependency, because the soak box is not
  guaranteed to have the dev extras installed.
* ``health_ms`` and ``monitor_ms`` are the two endpoints the soak is judged on:
  liveness, and the panel a human actually watches.
* ``ok`` is 0 when a probe failed; the runbook treats any 0 as a finding.

Usage:
    python agent/scripts/soak_watch.py --pid 1234 --url http://127.0.0.1:8000 \
        --hours 72 --interval 60 --out soak-72h.csv

Prefer ``--pid`` of the API process (``api_server``). Without a URL only the
process columns are sampled. Stop with Ctrl-C; the CSV stays readable.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEADER = ["ts", "rss_mb", "threads", "handles", "health_ms", "monitor_ms", "ok"]


# ─── process sampling ───


def _sample_windows(pid: int) -> tuple[float | None, int | None, int | None]:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TH32CS_SNAPTHREAD = 0x00000004
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    def _thread_count() -> int | None:
        """Count the thread table entries owned by *pid*."""
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or int(snapshot) == -1:
            return None
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            total = 0
            more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    total += 1
                more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            return total
        finally:
            kernel32.CloseHandle(snapshot)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, None, None
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        rss = None
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            rss = counters.WorkingSetSize / (1024 * 1024)
        count = wintypes.DWORD()
        # GetProcessHandleCount also exposes the handle table size, which is the
        # Windows analogue of an fd leak.
        handles = None
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            handles = int(count.value)
        return rss, _thread_count(), handles
    finally:
        kernel32.CloseHandle(handle)


def _sample_posix(pid: int) -> tuple[float | None, int | None, int | None]:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None, None, None
    rss = threads = None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) / 1024
        elif line.startswith("Threads:"):
            threads = int(line.split()[1])
    try:
        handles = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        handles = None
    return rss, threads, handles


def sample_process(pid: int) -> tuple[float | None, int | None, int | None]:
    if os.name == "nt":
        try:
            return _sample_windows(pid)
        except OSError:
            return None, None, None
    return _sample_posix(pid)


# ─── HTTP probes ───


def _probe(url: str, timeout: float = 5.0) -> tuple[float | None, bool]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(1)
            ok = response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return None, False
    return (time.perf_counter() - started) * 1000, ok


# ─── analysis ───

# A soak fails on *drift*, not on absolute size: a process that sits at 400 MB
# for three days is fine, one that gains 400 MB is not. These are the numbers
# the runbook signs off on.
RSS_GROWTH_PCT_BUDGET = 15.0     # first-quarter mean → last-quarter mean
HANDLE_GROWTH_BUDGET = 20        # Windows handles / POSIX fds, absolute
THREAD_GROWTH_BUDGET = 8
HEALTH_P95_BUDGET = 500.0        # ms, liveness must not degrade
PROBE_FAILURE_BUDGET = 0         # any failed probe is a finding


def _quartile_mean(values: list[float]) -> float | None:
    if not values:
        return None
    head = values[: max(1, len(values) // 4)]
    return sum(head) / len(head)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def analyze(path: Path) -> dict:
    """Summarise a soak CSV. Pure function of the file so CI can gate on it."""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        return {"samples": 0, "verdict": "empty"}

    def column(name: str) -> list[float]:
        out = []
        for row in rows:
            raw = (row.get(name) or "").strip()
            if raw:
                try:
                    out.append(float(raw))
                except ValueError:
                    continue
        return out

    rss = column("rss_mb")
    handles = column("handles")
    threads = column("threads")
    health = column("health_ms")
    monitor = column("monitor_ms")
    failures = sum(1 for row in rows if (row.get("ok") or "").strip() == "0")

    first_rss = _quartile_mean(rss)
    last_rss = _quartile_mean(list(reversed(rss)))
    growth_pct = None
    if first_rss and last_rss is not None:
        growth_pct = (last_rss - first_rss) / first_rss * 100.0

    handle_delta = (handles[-1] - handles[0]) if len(handles) >= 2 else None
    thread_delta = (threads[-1] - threads[0]) if len(threads) >= 2 else None
    health_p95 = _p95(health)

    findings = []
    if failures > PROBE_FAILURE_BUDGET:
        findings.append(f"{failures} probe failure(s)")
    if growth_pct is not None and growth_pct > RSS_GROWTH_PCT_BUDGET:
        findings.append(f"RSS grew {growth_pct:.1f}% (budget {RSS_GROWTH_PCT_BUDGET}%)")
    if handle_delta is not None and handle_delta > HANDLE_GROWTH_BUDGET:
        findings.append(f"handle/fd growth {handle_delta:+.0f} (budget {HANDLE_GROWTH_BUDGET})")
    if thread_delta is not None and thread_delta > THREAD_GROWTH_BUDGET:
        findings.append(f"thread growth {thread_delta:+.0f} (budget {THREAD_GROWTH_BUDGET})")
    if health_p95 is not None and health_p95 > HEALTH_P95_BUDGET:
        findings.append(f"/health p95 {health_p95:.0f}ms (budget {HEALTH_P95_BUDGET:.0f}ms)")

    return {
        "samples": len(rows),
        "window": [rows[0].get("ts"), rows[-1].get("ts")],
        "probe_failures": failures,
        "rss_first_quarter_mb": None if first_rss is None else round(first_rss, 1),
        "rss_last_quarter_mb": None if last_rss is None else round(last_rss, 1),
        "rss_growth_pct": None if growth_pct is None else round(growth_pct, 2),
        "handle_delta": handle_delta,
        "thread_delta": thread_delta,
        "health_p95_ms": None if health_p95 is None else round(health_p95, 1),
        "monitor_p95_ms": None if (value := _p95(monitor)) is None else round(value, 1),
        "verdict": "pass" if not findings else "fail",
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample a running API for a long soak.")
    parser.add_argument("--pid", type=int, help="API process id (required unless --analyze)")
    parser.add_argument("--url", default="", help="base URL, e.g. http://127.0.0.1:8000")
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between samples")
    parser.add_argument("--out", default="soak.csv")
    parser.add_argument(
        "--analyze",
        metavar="CSV",
        help="Do not sample; summarise an existing CSV and exit 1 when the soak drifted.",
    )
    args = parser.parse_args(argv)

    if args.analyze:
        report = analyze(Path(args.analyze))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report.get("verdict") == "pass" else 1

    if args.pid is None:
        parser.error("--pid is required unless --analyze is used")

    out = Path(args.out)
    new_file = not out.exists()
    deadline = time.time() + args.hours * 3600
    base = args.url.rstrip("/")

    with out.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(HEADER)
        try:
            while time.time() < deadline:
                rss, threads, handles = sample_process(args.pid)
                health_ms = monitor_ms = None
                ok = True
                if base:
                    health_ms, health_ok = _probe(f"{base}/health")
                    monitor_ms, monitor_ok = _probe(f"{base}/monitor")
                    ok = health_ok and monitor_ok
                if not ok:
                    health_ms = -1.0
                writer.writerow(
                    [
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "" if rss is None else f"{rss:.1f}",
                        "" if threads is None else threads,
                        "" if handles is None else handles,
                        "" if health_ms is None else f"{health_ms:.1f}",
                        "" if monitor_ms is None else f"{monitor_ms:.1f}",
                        1 if ok else 0,
                    ]
                )
                handle.flush()
                print(json.dumps({"ts": time.time(), "rss_mb": rss, "ok": ok}), flush=True)
                time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            print("stopped by user", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
