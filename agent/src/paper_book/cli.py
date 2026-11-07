"""Paper-book CLI.

One session::

    python -m src.paper_book step --date YYYY-MM-DD --run-id <id>

Twenty consecutive A-share sessions (weekends and holidays skipped), with a
window report of NAV, fills, unfilled orders, turnover, and cumulative
deviation versus the bound backtest::

    python -m src.paper_book soak --days 20 --end YYYY-MM-DD --run-id <id>
    python -m src.paper_book soak --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>
    python -m src.paper_book show --book-id <id> --window
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.monitor.models import StrategyRef
from src.monitor.store import board_id_for_strategy, load_board_for_strategy, load_monitor, save_board
from src.paper_book.book import resolve_run_dir, step_day
from src.paper_book.soak import calendar_kind, load_signal_schedule, plan_window, step_range
from src.paper_book.store import (
    PaperBookError,
    load_report,
    load_state,
    load_window,
    paper_book_root,
    require_book_id,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_book",
        description=(
            "Default A-share endpoint: lake → hypothesis → cost backtest → "
            "validation gate → tradable → paper book. "
            "prop_* prompts and the custom factor zoo are research-only."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    step = sub.add_parser("step", help="Simulate one session, or a from/to range of sessions")
    _add_common(step)
    step.add_argument("--date", default="", help="One session, YYYY-MM-DD")
    step.add_argument("--from", dest="start", default="", help="First calendar day of a range")
    step.add_argument("--to", dest="end", default="", help="Last calendar day of a range")

    soak = sub.add_parser(
        "soak",
        help="Settle consecutive trading sessions and write the window report",
    )
    _add_common(soak)
    soak.add_argument("--from", dest="start", default="", help="First calendar day, inclusive")
    soak.add_argument("--to", "--end", dest="end", default="", help="Last calendar day, inclusive")
    soak.add_argument(
        "--days",
        type=int,
        default=None,
        help="Session count ending on --end/--to. Default 20 when --from is omitted",
    )

    show = sub.add_parser("show", help="Print saved state, the latest daily report, and the window")
    show.add_argument("--book-id", required=True)
    show.add_argument("--root", default="")
    show.add_argument("--date", default="", help="Report date. Default: latest settled session")
    show.add_argument("--window", action="store_true", help="Include the latest window report")

    args = parser.parse_args(argv)
    try:
        if args.command == "step":
            payload = _step_command(args)
        elif args.command == "soak":
            payload = _soak_command(args)
        else:
            payload = _show(args)
    except PaperBookError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", default="", help="Monitor run id. Default: the active board")
    parser.add_argument("--book-id", default="", help="Book id. Default: the run id")
    parser.add_argument("--root", default="", help="Override VIBE_TRADING_PAPER_BOOK_ROOT")
    parser.add_argument(
        "--signals",
        default="",
        help="JSON schedule of per-session suggestions, keyed by YYYY-MM-DD",
    )


def _step_command(args: argparse.Namespace) -> dict:
    has_date = bool((args.date or "").strip())
    has_range = bool((args.start or "").strip() or (args.end or "").strip())
    if has_date and has_range:
        raise PaperBookError("pass --date or --from/--to, not both")
    if has_date:
        return _step_one(args)
    if has_range:
        start, end, sessions = plan_window(start=args.start, end=args.end, days=None)
        return _run_window(args, start=start, end=end, sessions=sessions)
    raise PaperBookError("step needs --date or --from/--to")


def _soak_command(args: argparse.Namespace) -> dict:
    start, end, sessions = plan_window(start=args.start, end=args.end, days=args.days)
    return _run_window(args, start=start, end=end, sessions=sessions)


def _load_config(args: argparse.Namespace):
    run_id = (args.run_id or "").strip()
    if run_id:
        config = load_board_for_strategy(StrategyRef(type="run_dir", run_id=run_id))
    else:
        config = load_monitor()
        run_id = (config.strategy.run_id or "").strip()
    return config, run_id


def _step_one(args: argparse.Namespace) -> dict:
    config, run_id = _load_config(args)
    root = Path(args.root).expanduser() if args.root else None
    report = step_day(
        as_of=args.date,
        config=config,
        book_id=(args.book_id or "").strip() or None,
        run_dir=resolve_run_dir(run_id),
        root=root,
    )
    if report.downgraded:
        save_board(board_id_for_strategy(config.strategy), config)
    return report.to_dict()


def _run_window(
    args: argparse.Namespace,
    *,
    start: str,
    end: str,
    sessions: list[str] | None,
) -> dict:
    config, run_id = _load_config(args)
    root = Path(args.root).expanduser() if args.root else None
    signals = load_signal_schedule(args.signals) if (args.signals or "").strip() else None
    report = step_range(
        start=start,
        end=end,
        sessions=sessions,
        calendar=calendar_kind() if sessions is not None else None,
        config=config,
        book_id=(args.book_id or "").strip() or None,
        run_dir=resolve_run_dir(run_id),
        root=root,
        signals_by_day=signals,
    )
    if report.downgraded:
        save_board(board_id_for_strategy(config.strategy), config)
    return report.to_dict()


def _show(args: argparse.Namespace) -> dict:
    root = paper_book_root(Path(args.root).expanduser() if args.root else None)
    book_id = require_book_id(args.book_id)
    state = load_state(root, book_id)
    if state is None:
        raise PaperBookError(f"no paper book named {book_id}")
    day = (args.date or state.settled_through or "").strip()
    report = load_report(root, book_id, day) if day else None
    payload: dict = {"state": state.to_dict(), "report": report}
    if args.window:
        payload["window"] = load_window(root, book_id)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
