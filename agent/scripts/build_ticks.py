"""Collect tick-level trade direction into the lake.

Why a daily job and not a backfill
----------------------------------
The Tencent print feed serves **only the most recent session** — the ``d=``
parameter is accepted and ignored. There is no archive request that returns an
older day, so 主动买卖结构 can only be accumulated forward, one session per run.
Run this after 15:00 Beijing on trading days and the history grows itself.

Cost per run is pages, not credits: a liquid name is ~55–60 pages of 70 prints
(≈20 s per symbol). That makes a watchlist cheap and a 5000-name sweep
impractical, which is why ``--from-daily`` exists: it takes the liquid end of
the universe already stored in the lake instead of the whole market.

Usage::

    python -m scripts.build_ticks --symbols 600519.SH,000001.SZ
    python -m scripts.build_ticks --from-daily --limit 50
    python -m scripts.build_ticks --report-only

Output goes to the ``tick_agg`` dataset, one row per (symbol, session). The
row is what factor research wants: 主动买占比, 大单净额, 成交笔数, 平均单笔金额.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

import pandas as pd  # noqa: E402

from src.data import ticks as tick_store  # noqa: E402


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


def _liquid_universe(limit: int | None) -> list[str]:
    """Symbols already stored at 1D, ordered by turnover where available.

    Sampling the liquid end is deliberate: illiquid names have few prints, so
    their aggregates are noisy and their pages are mostly wasted requests.
    """
    from src.data import bars as bar_store

    frame = bar_store.load_bars("1D")
    if frame.empty or "symbol" not in frame.columns:
        return []
    latest = pd.to_datetime(frame["date"], errors="coerce").max()
    recent = frame[pd.to_datetime(frame["date"], errors="coerce") >= latest - pd.Timedelta(days=10)]
    if "amount" in recent.columns and recent["amount"].notna().any():
        ranked = (
            recent.groupby("symbol")["amount"].sum().sort_values(ascending=False).index.tolist()
        )
    else:
        ranked = sorted(recent["symbol"].dropna().unique().tolist())
    return ranked[: int(limit)] if limit else ranked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect tick trade-direction aggregates")
    parser.add_argument("--symbols", help="Comma-separated codes")
    parser.add_argument("--from-daily", action="store_true", help="Use the lake's 1D universe")
    parser.add_argument("--limit", type=int, default=None, help="Cap the universe size")
    parser.add_argument("--date", default=None, help="Session date to stamp (default: today)")
    parser.add_argument("--sleep", type=float, default=0.4, help="Pause between symbols")
    parser.add_argument("--max-pages", type=int, default=400, help="Pagination safety cap")
    parser.add_argument("--report-only", action="store_true", help="Print coverage and exit")
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary only")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Accept an unfinished session (data before 15:00 is incomplete)",
    )
    args = parser.parse_args(argv)

    if args.symbols:
        symbols = [_normalize_code(part) for part in args.symbols.split(",") if part.strip()]
    elif args.from_daily:
        symbols = _liquid_universe(args.limit)
    else:
        symbols = _liquid_universe(args.limit or 50)

    if not symbols:
        print("no symbols to process (pass --symbols or fill the lake with 1D bars)", file=sys.stderr)
        return 2

    if args.report_only:
        summary = tick_store.coverage()
        if args.json:
            print(summary.to_json(orient="records", date_format="iso", indent=2))
        else:
            print(summary.to_string(index=False) if not summary.empty else "ticks: nothing stored")
        return 0

    session_date = args.date
    if session_date is None:
        session_date = tick_store.today_session()

    if (
        not args.allow_partial
        and session_date == tick_store.today_session()
        and not tick_store.session_is_complete()
    ):
        print(
            f"session {session_date} is still open (before 15:00 Beijing); "
            "the aggregate would be partial. Re-run after the close or pass --allow-partial.",
            file=sys.stderr,
        )
        return 3

    if not args.json:
        print(f"collecting prints for {len(symbols)} symbols (session {session_date})")

    rows: list[dict] = []
    failed: list[str] = []
    started = time.time()
    for index, symbol in enumerate(symbols, 1):
        try:
            prints = tick_store.fetch_ticks(
                symbol, trade_date=session_date, max_pages=args.max_pages
            )
            row = tick_store.aggregate(prints, symbol, trade_date=session_date)
            if row:
                rows.append(row)
            else:
                failed.append(symbol)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the run
            failed.append(symbol)
            print(f"  {symbol}: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.sleep and index < len(symbols):
            time.sleep(args.sleep)
        if not args.json and index % 10 == 0:
            print(f"  {index}/{len(symbols)} done ({time.time() - started:.0f}s)")

    written = tick_store.save(rows)

    # Reconcile against the session's daily bar volume. A complete print run
    # lands within ~0.05%; a wider gap means pagination was cut short, and
    # without this check that would be indistinguishable from a quiet day.
    deviations: dict[str, float] = {}
    try:
        from src.data import bars as bar_store

        symbols_checked = [row["symbol"] for row in rows]
        bar_volumes: dict[str, float] = {}
        stored = bar_store.load_bars(
            "1D", symbols=symbols_checked, start=session_date, end=session_date
        )
        if not stored.empty and {"symbol", "volume"} <= set(stored.columns):
            bar_volumes = dict(zip(stored["symbol"], stored["volume"].astype(float)))
        missing = [sym for sym in symbols_checked if sym not in bar_volumes]
        if missing:
            # The lake may not hold daily bars for this session yet. One daily
            # request per missing symbol is cheap next to ~60 print pages, and
            # it keeps the reconciliation from silently skipping.
            from backtest.loaders.registry import LOADER_REGISTRY, _ensure_registered

            _ensure_registered()
            loader_cls = LOADER_REGISTRY.get("tencent")
            if loader_cls is not None:
                fetched = loader_cls().fetch(missing, session_date, session_date, interval="1D")
                for sym, frame in (fetched or {}).items():
                    if frame is not None and not frame.empty:
                        bar_volumes[sym] = float(frame["volume"].iloc[-1])
        for row in rows:
            deviation = tick_store.volume_deviation(row["volume"], bar_volumes.get(row["symbol"]))
            if deviation is not None:
                deviations[row["symbol"]] = round(deviation, 3)
    except Exception as exc:  # noqa: BLE001 — reconciliation is advisory
        print(f"note: volume reconciliation skipped ({exc})", file=sys.stderr)

    incomplete = [row["symbol"] for row in rows if not row.get("prints_complete", True)]
    off = {sym: dev for sym, dev in deviations.items() if abs(dev) > 1.0}

    summary = {
        "session": session_date,
        "symbols_requested": len(symbols),
        "symbols_with_prints": len(rows),
        "symbols_failed": failed[:10],
        "rows_written": int(written),
        "elapsed_s": round(time.time() - started, 1),
        "total_prints": int(sum(row["trades"] for row in rows)) if rows else 0,
        "incomplete_pagination": incomplete,
        "volume_deviation_pct": deviations,
        "volume_mismatch": off,
        "median_active_buy_ratio": (
            round(float(pd.Series([row["active_buy_ratio"] for row in rows]).median()), 4)
            if rows else None
        ),
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    print(
        f"stored {written} rows for {len(rows)}/{len(symbols)} symbols "
        f"({summary['total_prints']} prints, {summary['elapsed_s']}s)"
    )
    if summary["median_active_buy_ratio"] is not None:
        print(f"median active-buy ratio: {summary['median_active_buy_ratio']}")
    if deviations:
        print(f"volume vs daily bar: {deviations}")
    if incomplete:
        print(
            f"WARNING: pagination incomplete for {incomplete} — those rows understate "
            "the session; re-run to overwrite them",
            file=sys.stderr,
        )
    if off:
        print(f"WARNING: volume deviates >1% for {off}; treat as suspect", file=sys.stderr)
    if failed:
        print(f"note: {len(failed)} symbols had no prints (halted or suspended)", file=sys.stderr)
    print("this dataset grows one session per run; schedule it after the close.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
