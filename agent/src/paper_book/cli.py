"""Day-step CLI: ``python -m src.paper_book step --date YYYY-MM-DD``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.monitor.models import StrategyRef
from src.monitor.store import load_board_for_strategy, load_monitor, save_board, board_id_for_strategy
from src.paper_book.book import resolve_run_dir, step_day
from src.paper_book.store import PaperBookError, load_report, load_state, paper_book_root, require_book_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_book",
        description="Settle one A-share paper-book session from monitor tradable signals.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    step = sub.add_parser("step", help="Simulate one session and write the daily report")
    step.add_argument("--date", required=True, help="Session date YYYY-MM-DD")
    step.add_argument("--run-id", default="", help="Monitor run id. Default: the active board")
    step.add_argument("--book-id", default="", help="Book id. Default: the run id")
    step.add_argument("--root", default="", help="Override VIBE_TRADING_PAPER_BOOK_ROOT")

    show = sub.add_parser("show", help="Print saved state and the latest report")
    show.add_argument("--book-id", required=True)
    show.add_argument("--root", default="")
    show.add_argument("--date", default="", help="Report date. Default: latest settled session")

    args = parser.parse_args(argv)
    try:
        if args.command == "step":
            payload = _step(args)
        else:
            payload = _show(args)
    except PaperBookError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _step(args: argparse.Namespace) -> dict:
    run_id = (args.run_id or "").strip()
    if run_id:
        config = load_board_for_strategy(StrategyRef(type="run_dir", run_id=run_id))
    else:
        config = load_monitor()
        run_id = (config.strategy.run_id or "").strip()
    run_dir = resolve_run_dir(run_id)
    root = Path(args.root).expanduser() if args.root else None
    report = step_day(
        as_of=args.date,
        config=config,
        book_id=(args.book_id or "").strip() or None,
        run_dir=run_dir,
        root=root,
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
    return {"state": state.to_dict(), "report": report}


if __name__ == "__main__":
    raise SystemExit(main())
