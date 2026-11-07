#!/usr/bin/env python
"""Run a dataset ingest job from the command line.

The console's "补齐" button is the same runner with a bounded budget; this entry
point exists for the unattended case — a nightly job, or the first five-year
backfill that a UI job deliberately refuses to attempt in one go.

    python scripts/ingest.py --list
    python scripts/ingest.py bars_daily --all                  # 全市场五年回补
    python scripts/ingest.py bars_daily --all --years 3        # 只补三年
    python scripts/ingest.py bars_daily --start 2021-01-01 --end 2026-10-30
    python scripts/ingest.py bars_daily --limit 500            # incremental
    python scripts/ingest.py bars_daily --mode adj_factor      # 补复权因子（原地修复）
    python scripts/ingest.py tradability                       # panel from bars
    python scripts/ingest.py calendar
    python scripts/ingest.py --jobs                            # 最近任务与断点
    python scripts/ingest.py --cancel 3f9a1c                   # 取消一个在跑的任务

Exit code is 0 on success, 1 when the job failed or was cancelled, so cron/CI
can tell them apart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_env() -> None:
    """Make ``agent/.env`` visible to the CLI.

    The API server reads it during startup, but this entry point did not — so a
    token-backed plan (Tushare daily, the adj_factor repair) silently degraded to
    "no token configured" exactly when an operator ran it by hand to fix
    something.  A variable already present in the real environment still wins.
    """
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - optional in a minimal install
        return
    load_dotenv(env_file)


_load_env()

from src.data import ingest  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a data-lake ingest job")
    parser.add_argument("dataset", nargs="?", help="dataset name, e.g. bars_daily")
    parser.add_argument("--list", action="store_true", help="list ingestible datasets")
    parser.add_argument("--start", help="inclusive start YYYY-MM-DD")
    parser.add_argument("--end", help="inclusive end YYYY-MM-DD")
    parser.add_argument("--symbols", help="comma separated symbol whitelist")
    parser.add_argument("--limit", type=int, help="cap on symbols")
    parser.add_argument("--interval", help="bar interval, e.g. 5m")
    parser.add_argument(
        "--all",
        action="store_true",
        help="全市场标的池（默认按 --years 回溯，供五年回补使用）",
    )
    parser.add_argument(
        "--years",
        type=float,
        default=5.0,
        help="with --all and no --start: how far back to reach (default 5)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="concurrent fetches for per-symbol sources (default 8)",
    )
    parser.add_argument("--refresh", action="store_true", help="re-pull a covered window")
    parser.add_argument(
        "--mode",
        default=None,
        help="bars_daily 子模式：bars（默认行情）| adj_factor（只补复权因子）",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=3600.0,
        help="soft wall-clock budget in seconds (default 3600)",
    )
    parser.add_argument("--json", action="store_true", help="print the raw job payload")
    parser.add_argument(
        "--jobs", action="store_true", help="list recent jobs with their cursors"
    )
    parser.add_argument(
        "--cancel", metavar="JOB_ID", help="ask a running job to stop at its next unit"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="do not print live progress on stderr"
    )
    return parser


def _watch(job: dict, *, quiet: bool) -> dict:
    """Poll one submitted job until it finishes, narrating progress on stderr.

    A full-market backfill takes minutes; the synchronous runner would sit
    silent for all of them and look hung.  Progress goes to stderr so ``--json``
    still yields a clean document on stdout.
    """
    import time

    seen = ""
    while True:
        current = ingest.job(job["id"]) or job
        message = str(current.get("message") or "")
        percent = float(current.get("progress") or 0.0) * 100.0
        line = f"  {current['status']:<7} {percent:5.1f}%  {message}"
        if not quiet and line != seen:
            seen = line
            print("\r" + line[:110].ljust(110), end="", flush=True, file=sys.stderr)
        if current["status"] in {"done", "failed", "cancelled"}:
            if not quiet:
                print(file=sys.stderr)
            return current
        time.sleep(1.0)


def _list_jobs(limit: int = 20) -> None:
    """Recent jobs with the two columns that make a resume legible."""
    for item in ingest.jobs(limit):
        cursor = item.get("cursor") or {}
        progress = ""
        if cursor.get("total"):
            progress = f" {cursor.get('done') or 0}/{cursor['total']}"
        elif item.get("total"):
            progress = f" {item.get('done') or 0}/{item['total']}"
        print(
            f"{item['id']:<13} {item['dataset']:<16} {item['status']:<9}"
            f" {item.get('queued_at') or '':<19}{progress}  {item.get('message') or ''}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.jobs:
        _list_jobs()
        return 0

    if args.cancel:
        try:
            item = ingest.cancel(args.cancel)
        except ingest.IngestError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        print(f"[{item.get('status')}] {item.get('dataset')} — {item.get('message')}")
        # A cancel is a request: the job stops at its next unit boundary, and a
        # second process cannot wait on a thread it does not own.
        return 0

    if args.list or not args.dataset:
        for item in ingest.dataset_options():
            flag = "可入库" if item["runnable"] else f"跳过（{item['reason']}）"
            params = f" 参数: {', '.join(item['params'])}" if item["params"] else ""
            print(f"{item['name']:<16} {item['label']:<10} {flag}{params}")
        return 0

    start = args.start
    if args.all and not start:
        # A full backfill is expressed in years, not dates: "five years of
        # history" is the registry's retention contract, and spelling it as a
        # start date in the shell is how the two drift apart.
        import datetime as dt

        from src.data import hub

        end_day = (
            dt.date.fromisoformat(args.end)
            if args.end
            else dt.date.fromisoformat(str(hub.resolve_as_of().date))
        )
        start = (end_day - dt.timedelta(days=int(365 * max(args.years, 0.1)))).isoformat()

    params = {
        "start": start,
        "end": args.end,
        "symbols": [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None,
        "limit": args.limit,
        "interval": args.interval,
        "refresh": args.refresh or None,
        "universe_mode": "full" if args.all else None,
        "workers": args.workers if args.all or args.symbols else None,
        "mode": args.mode,
    }
    try:
        queued = ingest.submit(args.dataset, deadline_s=args.deadline, **params)
    except ingest.IngestBusy as exc:
        print(f"繁忙：{exc}", file=sys.stderr)
        return 1
    except ingest.IngestError as exc:
        print(f"无法入库：{exc}", file=sys.stderr)
        return 1
    payload = _watch(queued, quiet=args.quiet)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        report = payload.get("report") or {}
        print(f"[{payload['status']}] {payload['dataset']} — {payload['message']}")
        if report:
            print(
                f"  行 {report.get('rows')} / 交易日 {report.get('sessions')} / "
                f"标的 {report.get('symbols')} / 来源 {report.get('source') or '—'} / "
                f"区间 {report.get('start')} → {report.get('end')}"
            )
            for note in report.get("notes") or []:
                print(f"  · {note}")
        cursor = payload.get("cursor") or {}
        if cursor:
            print(f"  断点：{json.dumps(cursor, ensure_ascii=False)}")
        if payload.get("error"):
            print(f"  错误：{payload['error']}", file=sys.stderr)
    return 0 if payload["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
