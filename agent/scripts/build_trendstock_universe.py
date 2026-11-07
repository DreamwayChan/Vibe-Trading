"""Build the BK1715 趋势股 universe run from the local lake (offline).

Reads the 2026-11-03 market-pick member cache for BK1715, pulls daily bars for
2024-01-01..2026-11-03 out of ``~/.vibe-trading/lake/bars_daily`` and writes the
``universe`` run layout the sector research scripts expect:

    runs/<run_id>/universe.csv
    runs/<run_id>/universe_meta.json
    runs/<run_id>/config.json
    runs/<run_id>/artifacts/ohlcv_<code>.csv

Run from ``agent/``:
    python scripts/build_trendstock_universe.py
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from src.data import bars as bar_store  # noqa: E402
from src.data.lake import read_dataset  # noqa: E402

RUN_ID = "20261103_201500_00_trendstock_universe"
RUN_DIR = _AGENT_ROOT / "runs" / RUN_ID
MEMBERS = Path.home() / ".vibe-trading" / "market_pick" / "members" / "2026-11-03" / "BK1715.json"
BK = "BK1715"
PANEL = "趋势股 BK1715"
START_DATE = "2024-01-01"
END_DATE = "2026-11-03"
MAX_NAMES = 80


def _known_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for path in glob.glob(str(_AGENT_ROOT / "runs" / "*" / "universe.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            continue
        if {"ts_code", "name"}.issubset(df.columns):
            for code, name in zip(df.ts_code, df.name):
                if isinstance(name, str) and name.strip():
                    names.setdefault(str(code), name.strip())
    for path in glob.glob(str(_AGENT_ROOT / "runs" / "*" / "universe_meta.json")):
        try:
            meta = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for code, name in (meta.get("names") or {}).items():
            if isinstance(name, str) and name.strip():
                names.setdefault(str(code), name.strip())
    return names


def main() -> None:
    codes = [str(c) for c in json.loads(MEMBERS.read_text(encoding="utf-8"))]
    codes = [c for c in codes if not c.endswith(".BJ")][:MAX_NAMES]
    print(f"[universe] {BK} members={len(codes)}")

    frame = read_dataset(
        bar_store.DATASET_DAILY,
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "amount"],
        filters={"year": ["2024", "2025", "2026"]},
    )
    frame = frame[frame["symbol"].isin(codes)].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[(frame["date"] >= START_DATE) & (frame["date"] <= END_DATE)]

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "artifacts").mkdir(parents=True, exist_ok=True)
    for path in (RUN_DIR / "artifacts").glob("ohlcv_*.csv"):
        path.unlink()

    names = _known_names()
    kept: list[str] = []
    for code, sub in frame.groupby("symbol"):
        sub = sub.sort_values("date").drop_duplicates("date")
        if len(sub) < 120:
            print(f"  [skip] {code}: only {len(sub)} bars")
            continue
        out = sub[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
        out.to_csv(RUN_DIR / "artifacts" / f"ohlcv_{code}.csv", index=False)
        kept.append(str(code))

    universe = pd.DataFrame(
        {
            "ts_code": kept,
            "name": [names.get(c, "") for c in kept],
        }
    )
    universe.to_csv(RUN_DIR / "universe.csv", index=False, encoding="utf-8-sig")
    (RUN_DIR / "universe_meta.json").write_text(
        json.dumps(
            {
                "panel": PANEL,
                "bk": BK,
                "board_name": "趋势股",
                "board_type": "concept",
                "as_of": "2026-11-03",
                "market_pick_rank": 2,
                "universe": f"东财概念「趋势股」{BK}（排除北交所；扫描成分 {len(codes)} 只）",
                "source": "lake",
                "start_date": START_DATE,
                "end_date": END_DATE,
                "names": {c: names.get(c, "") for c in kept},
                "bars": {c: int((frame["symbol"] == c).sum()) for c in kept},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (RUN_DIR / "config.json").write_text(
        json.dumps(
            {
                "codes": sorted(kept),
                "source": "tushare",
                "start_date": START_DATE,
                "end_date": END_DATE,
                "interval": "1D",
                "initial_cash": 1000000.0,
                "commission": 0.0003,
                "stamp_duty": 0.001,
                "slippage": 0.0005,
                "parameters": {"panel": PANEL, "universe": f"{BK} 趋势股"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[done] {RUN_DIR} symbols={len(kept)} named={sum(1 for c in kept if names.get(c))}")


if __name__ == "__main__":
    main()
