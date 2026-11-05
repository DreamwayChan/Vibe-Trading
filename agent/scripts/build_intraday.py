"""Backfill intraday bars into the local lake.

Why a script instead of a loader flag
-------------------------------------
The free A-share intraday feeds are windows, not archives.  Tencent's
``mkline`` returns the latest 320 bars and ignores the requested range; Sina
returns a few thousand; Eastmoney caps 1-minute history at a few sessions.  No
single request can produce a year of 5-minute bars.

Running this job on a schedule is what turns those windows into history: each
run upserts ``(symbol, interval, timestamp)`` into the lake, so today's 320 bars
and tomorrow's 320 bars overlap and extend.  ``--report`` shows the accumulated
span, which is the number to watch — not what one request returned.

Usage::

    python -m scripts.build_intraday --symbols 600519.SH,000001.SZ --interval 5m
    python -m scripts.build_intraday --limit 50 --interval 5m --start 2026-09-01
    python -m scripts.build_intraday --interval 1H --report-only

Notes
-----
* ``--start`` defaults to the lake's own coverage per symbol, so a daily rerun
  only asks for what is missing.
* The chosen interval decides which loaders are eligible: a 1-minute request
  skips BaoStock (no 1m feed) instead of receiving daily bars from it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

# Token-gated sources (Tushare) read their keys from the environment, and the
# backtest runner loads ``.env`` at import time.  A standalone script that skips
# this step reports "no tokens configured" for a source that actually works.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pandas as pd  # noqa: E402

from src.data import bars as bar_store  # noqa: E402


def _normalize_code(code: str) -> str:
    """Map a bare 6-digit code to its exchange suffix."""
    code = str(code).strip().upper()
    if "." in code:
        return code
    if code.startswith(("60", "68", "90")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "20")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88")):
        return f"{code}.BJ"
    return code


def _universe(limit: int | None) -> list[str]:
    import akshare as ak

    frame = ak.stock_info_a_code_name()
    code_col = "code" if "code" in frame.columns else frame.columns[0]
    codes = [_normalize_code(c) for c in frame[code_col]]
    return codes[: int(limit)] if limit else codes


def _pick_loader(interval: str, requested: str):
    """Return a loader instance that can actually serve *interval*."""
    from backtest.loaders._intervals import supports_interval
    from backtest.loaders.registry import LOADER_REGISTRY, _ensure_registered

    _ensure_registered()
    if requested != "auto":
        loader_cls = LOADER_REGISTRY.get(requested)
        if loader_cls is None:
            raise SystemExit(f"unknown source {requested!r}")
        if not supports_interval(loader_cls, interval):
            available = sorted(
                name for name, cls in LOADER_REGISTRY.items()
                if supports_interval(cls, interval)
            )
            raise SystemExit(
                f"source {requested!r} cannot serve interval {interval!r}; "
                f"try one of {available}"
            )
        return loader_cls()

    from backtest.loaders.registry import resolve_loader

    return resolve_loader("a_share", interval=interval)


def _resume_start(symbols: list[str], interval: str) -> str | None:
    """Latest stored date per symbol → the next fetch only asks for the gap."""
    stored = bar_store.coverage(interval, symbols=symbols)
    if stored.empty:
        return None
    return (stored["last"].max() + pd.Timedelta(days=1)).date().isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill intraday bars into the lake")
    parser.add_argument("--symbols", help="Comma-separated codes")
    parser.add_argument("--limit", type=int, default=None, help="Cap the universe size")
    parser.add_argument("--interval", default="5m", help="Bar size (default: 5m)")
    parser.add_argument("--start", default=None, help="First date; default: resume from lake")
    parser.add_argument("--end", default=None, help="Last date (default: today)")
    parser.add_argument("--source", default="auto", help="Loader name, or 'auto'")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=30,
        help="Window when --start is absent and the lake is empty",
    )
    parser.add_argument("--report-only", action="store_true", help="Print coverage and exit")
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary only")
    args = parser.parse_args(argv)

    if args.symbols:
        symbols = [_normalize_code(part) for part in args.symbols.split(",") if part.strip()]
    else:
        symbols = _universe(args.limit)

    if args.report_only:
        summary = bar_store.coverage(args.interval, symbols=symbols)
        if args.json:
            print(summary.to_json(orient="records", date_format="iso", indent=2))
        else:
            print(summary.to_string(index=False) if not summary.empty else "lake: no stored bars")
        return 0

    end = args.end or dt.date.today().isoformat()
    start = args.start or _resume_start(symbols, args.interval)
    if start is None:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=args.lookback_days)).isoformat()

    loader = _pick_loader(args.interval, args.source)
    source_name = getattr(loader, "name", args.source)
    if not args.json:
        print(f"fetching {len(symbols)} symbols {args.interval} {start} .. {end} via {source_name}")

    # stk_mins is day-capped (2 calls/token on a 120-point account), so say how
    # many captures are actually possible today instead of failing per symbol.
    if source_name == "tushare" and args.interval.upper() != "1D":
        try:
            from backtest.loaders.tushare_pool import get_tushare_pool

            quota = get_tushare_pool().quota_report().get("stk_mins")
        except Exception:  # noqa: BLE001 — quota visibility is advisory
            quota = None
        if quota:
            budget = quota["per_token"] * quota["tokens"]
            print(
                f"tushare stk_mins allowance: {quota['remaining']}/{budget} calls today "
                f"({quota['tokens']} tokens x {quota['per_token']}/day) — a per-hour "
                "block can still refuse them"
            )
            if quota["remaining"] == 0:
                print(
                    "stk_mins daily quota is spent (resets at Beijing midnight); "
                    "use --source tencent for the recent window or wait.",
                    file=sys.stderr,
                )
                return 4
            if len(symbols) > quota["remaining"]:
                print(
                    f"note: {len(symbols)} symbols requested but only {quota['remaining']} "
                    "calls remain today; the tail will be skipped",
                    file=sys.stderr,
                )

    frames = loader.fetch(symbols, start, end, interval=args.interval)
    frames = {sym: f for sym, f in frames.items() if f is not None and not f.empty}

    written = bar_store.save_bars(frames, args.interval, source=source_name)
    summary_frame = bar_store.coverage(args.interval, symbols=symbols)

    summary = {
        "interval": args.interval,
        "source": source_name,
        "window": [start, end],
        "symbols_requested": len(symbols),
        "symbols_fetched": len(frames),
        "bars_fetched": int(sum(len(f) for f in frames.values())),
        "rows_written": int(written),
        "stored_symbols": int(summary_frame["symbol"].nunique()) if not summary_frame.empty else 0,
        "stored_span": (
            [str(summary_frame["first"].min().date()), str(summary_frame["last"].max().date())]
            if not summary_frame.empty else []
        ),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"fetched {summary['bars_fetched']} bars for {summary['symbols_fetched']}/{len(symbols)} symbols")
        print(f"lake: {written} rows written; stored span {summary['stored_span']}")
        if summary["symbols_fetched"] < len(symbols):
            print(
                "note: a short result is expected for old windows — the free "
                "intraday feeds only serve recent bars. Re-run tomorrow to extend.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
