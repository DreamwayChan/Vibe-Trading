"""Build the A-share tradability panel into the local lake.

What it does
------------
1. Resolve a universe (AKShare's A-share list, or ``--symbols``).
2. Fetch daily bars through the normal loader chain.
3. Expand every symbol across the exchange sessions from
   :mod:`src.data.calendar`, so halted days get explicit rows instead of
   silently disappearing.
4. Derive halt flags, ST designation, board, and exact limit prices.
5. Upsert into ``<lake>/tradability/year=YYYY/``.

Usage::

    python -m scripts.build_tradability --limit 50 --start 2024-01-01 --end 2024-12-31
    python -m scripts.build_tradability --symbols 600519.SH,000001.SZ --start 2024-06-01
    python -m scripts.build_tradability --universe akshare --refresh-calendar

The runner attaches this panel to ``ChinaAEngine`` by default (halts and the
exchange price band, including ST).  Set ``VIBE_TRADING_TRADABILITY_PANEL=0``
to keep the close-to-close heuristic so a comparison with pre-panel runs stays
reproducible.  Zero-volume bars stay unfillable unless
``VIBE_TRADING_ASHARE_BLOCK_SUSPENSION=0``.
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

import pandas as pd  # noqa: E402

from src.data import calendar as cal  # noqa: E402
from src.data import tradability as td  # noqa: E402


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


def fetch_universe(limit: int | None = None) -> pd.DataFrame:
    """Return ``symbol / name / is_st / board`` for the A-share universe."""
    import akshare as ak

    frame = ak.stock_info_a_code_name()
    code_col = "code" if "code" in frame.columns else frame.columns[0]
    name_col = "name" if "name" in frame.columns else frame.columns[1]
    universe = pd.DataFrame(
        {
            "symbol": [_normalize_code(c) for c in frame[code_col]],
            "name": frame[name_col].astype(str),
        }
    )
    universe["is_st"] = universe["name"].str.upper().str.contains("ST", na=False)
    universe["board"] = universe["symbol"].map(td.board_of)
    if limit:
        universe = universe.head(int(limit))
    return universe.reset_index(drop=True)


def parse_symbols(raw: str) -> pd.DataFrame:
    codes = [_normalize_code(part) for part in raw.split(",") if part.strip()]
    return pd.DataFrame(
        {
            "symbol": codes,
            "name": codes,
            "is_st": [False] * len(codes),
            "board": [td.board_of(c) for c in codes],
        }
    )


def fetch_bars(symbols: list[str], start: str, end: str, source: str) -> dict[str, pd.DataFrame]:
    """Fetch daily bars through the loader registry, skipping failures."""
    from backtest.loaders.registry import get_loader_cls_with_fallback

    loader_cls = get_loader_cls_with_fallback(source)
    loader = loader_cls()
    bars = loader.fetch(symbols, start, end, interval="1D")
    return {symbol: frame for symbol, frame in bars.items() if frame is not None and not frame.empty}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the A-share tradability panel")
    parser.add_argument("--symbols", help="Comma-separated codes; bypasses the universe fetch")
    parser.add_argument(
        "--universe",
        default="akshare",
        choices=["akshare", "none"],
        help="Universe source (default: AKShare's A-share list)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the universe size (smoke tests)")
    parser.add_argument("--start", default="2024-01-01", help="First session (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Last session (default: today)")
    parser.add_argument("--source", default="akshare", help="Loader name for bars (default: akshare)")
    parser.add_argument("--refresh-calendar", action="store_true", help="Force a calendar re-seed first")
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary only")
    args = parser.parse_args(argv)

    end = args.end or dt.date.today().isoformat()

    if args.refresh_calendar:
        seeded = cal.refresh_calendar()
        if not args.json:
            print(f"calendar: {seeded} sessions, source={cal.calendar_source()}")

    if args.symbols:
        universe = parse_symbols(args.symbols)
    elif args.universe == "akshare":
        universe = fetch_universe(args.limit)
    else:
        parser.error("provide --symbols or --universe akshare")

    symbols = universe["symbol"].tolist()
    if not args.json:
        print(f"universe: {len(symbols)} symbols, {args.start} .. {end}")

    sessions = cal.trading_days(args.start, end)
    if not sessions:
        print("no sessions in range", file=sys.stderr)
        return 2
    if not args.json:
        print(f"sessions: {len(sessions)} ({sessions[0]} .. {sessions[-1]})")

    bars = fetch_bars(symbols, args.start, end, args.source)
    if not args.json:
        print(f"bars: {len(bars)}/{len(symbols)} symbols resolved")

    panel = td.build_panel(bars, universe=universe, sessions=sessions)
    if panel.empty:
        print("panel empty: no bars resolved", file=sys.stderr)
        return 1

    written = td.save_panel(panel)
    from src.data.lake import lake_root

    summary = {
        "rows_written": written,
        "symbols": int(panel["symbol"].nunique()),
        "symbols_requested": len(symbols),
        "sessions": len(sessions),
        "date_range": [str(panel["date"].min()), str(panel["date"].max())],
        "halted_rows": int(panel["is_suspended"].sum()),
        "st_symbols": int(panel.loc[panel["is_st"], "symbol"].nunique()),
        "band_mismatch_rows": int(panel["band_mismatch"].sum()),
        "lake_root": str(lake_root()),
        "enable_backtest_flag": "VIBE_TRADING_TRADABILITY_PANEL=1 (default; set 0 to disable)",
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"panel: {written} rows, {summary['symbols']} symbols, "
            f"halted={summary['halted_rows']}, st_symbols={summary['st_symbols']}, "
            f"band_mismatch={summary['band_mismatch_rows']}"
        )
        if summary["band_mismatch_rows"]:
            print(
                "note: band_mismatch>0 means the source series is adjusted "
                "(dividend/split) — the engine will keep the halt flag but fall "
                "back to its price-move heuristic on those rows. Use an "
                "unadjusted source (e.g. --source tushare) for exact bands."
            )
        print(f"enable in backtests with {summary['enable_backtest_flag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
