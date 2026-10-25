"""Walk-forward the long-only 3-state rule on current monitor holdings.

Uses the same 2018–2026 daily tape as research_long_trend_holdings.py.
Evaluates: does gating the bearish prior on CSI 300 bull, and treating
chop as buy-dip / sell-rally, beat always-down / always-long?

声明：研究/回测，非实盘建议。
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))
if str(_AGENT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT / "src"))

os.environ.setdefault("VIBE_TRADING_DATA_CACHE", "1")

from src.providers.llm import _ensure_dotenv  # noqa: E402

_ensure_dotenv()

from backtest.loaders.base import loader_cache_forced  # noqa: E402
from backtest.runner import fetch_data_map  # noqa: E402
from src.monitor.trend_regime import (  # noqa: E402
    classify_structure,
    classify_swing,
    long_only_plan,
)

if str(_AGENT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT / "scripts"))
from research_long_trend_holdings import (  # noqa: E402
    END_DATE,
    MARKET,
    MIN_BARS,
    START_DATE,
    STEP,
    load_holdings,
)

OUT_PATH = Path.home() / ".vibe-trading" / "cache" / "long_trend_regime_result.json"
FWD = (20, 60)


def _close(frame: pd.DataFrame) -> pd.Series:
    series = frame["close"].astype(float)
    if "trade_date" in frame.columns:
        series.index = pd.to_datetime(frame["trade_date"])
    return series


def fetch_all(symbols: list[str]) -> dict[str, pd.DataFrame]:
    codes = list(dict.fromkeys(symbols + [MARKET]))
    with loader_cache_forced(True):
        result = fetch_data_map(
            {
                "source": "auto",
                "codes": codes,
                "start_date": START_DATE,
                "end_date": END_DATE,
                "interval": "1D",
            },
            allow_partial=True,
        )
    data_map = getattr(result, "data_map", None) or {}
    out: dict[str, pd.DataFrame] = {}
    for code, frame in data_map.items():
        if frame is None or getattr(frame, "empty", True):
            continue
        df = frame.copy()
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.sort_values("trade_date").drop_duplicates("trade_date")
            df = df.set_index("trade_date")
        else:
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
        if "close" in df.columns:
            out[str(code).upper()] = df
    return out


def _fwd_ret(close: pd.Series, i: int, horizon: int) -> float | None:
    if i + horizon >= len(close):
        return None
    start = float(close.iloc[i])
    end = float(close.iloc[i + horizon])
    if start <= 0 or not np.isfinite(start) or not np.isfinite(end):
        return None
    return end / start - 1.0


def _acc(flags: list[bool]) -> float | None:
    if not flags:
        return None
    return sum(1 for x in flags if x) / len(flags)


def main() -> None:
    holdings = load_holdings()
    symbols = [h["symbol"] for h in holdings]
    board_of = {h["symbol"]: h["board"] for h in holdings}
    data = fetch_all(symbols)
    mkt = data.get(MARKET)
    if mkt is None:
        raise SystemExit("CSI 300 missing")
    mkt_close = mkt["close"].astype(float)
    mkt_idx = list(mkt.index)

    # Index regime on its own calendar, keyed by date for nearest-day join.
    mkt_by_date: dict[pd.Timestamp, object] = {}
    for i in range(MIN_BARS, len(mkt_close)):
        snap = classify_structure(mkt_close.iloc[: i + 1])
        if snap is not None:
            mkt_by_date[pd.Timestamp(mkt_idx[i]).normalize()] = snap

    rows = []
    live = []
    for symbol, frame in data.items():
        if symbol == MARKET or symbol not in board_of:
            continue
        close = frame["close"].astype(float)
        n = len(close)
        last_stock = classify_structure(close)
        last_swing = classify_swing(close)
        last_mkt = mkt_by_date.get(pd.Timestamp(frame.index[-1]).normalize())
        if last_mkt is None and mkt_by_date:
            nearest = min(mkt_by_date, key=lambda t: abs((t - pd.Timestamp(frame.index[-1])).days))
            if abs((nearest - pd.Timestamp(frame.index[-1])).days) <= 3:
                last_mkt = mkt_by_date[nearest]
        if last_stock and last_swing and last_mkt:
            plan = long_only_plan(last_mkt, last_stock, last_swing)
            live.append(
                {
                    "symbol": symbol,
                    "board": board_of[symbol],
                    "as_of": str(pd.Timestamp(frame.index[-1]).date()),
                    **plan.to_dict(),
                    "stock_mom120": last_stock.mom120,
                    "market_mom120": last_mkt.mom120,
                }
            )
        for i in range(MIN_BARS, n, STEP):
            stock = classify_structure(close.iloc[: i + 1])
            swing = classify_swing(close.iloc[: i + 1])
            ts = pd.Timestamp(frame.index[i]).normalize()
            market = mkt_by_date.get(ts)
            if market is None and mkt_by_date:
                nearest = min(mkt_by_date, key=lambda t: abs((t - ts).days))
                if abs((nearest - ts).days) <= 3:
                    market = mkt_by_date[nearest]
            if stock is None or swing is None or market is None:
                continue
            plan = long_only_plan(market, stock, swing)
            rec = {
                "symbol": symbol,
                "board": board_of[symbol],
                "date": str(ts.date()),
                "market": plan.market_regime,
                "stock": plan.stock_regime,
                "swing": plan.swing,
                "action": plan.action,
                "fade": plan.fade_rallies,
            }
            ok = True
            for h in FWD:
                ret = _fwd_ret(close, i, h)
                if ret is None:
                    ok = False
                    break
                rec[f"ret{h}"] = ret
                rec[f"up{h}"] = ret > 0
            if ok:
                rows.append(rec)

    def subset(pred) -> list[dict]:
        return [r for r in rows if pred(r)]

    def pack(items: list[dict]) -> dict:
        if not items:
            return {"n": 0}
        r20 = [r["ret20"] for r in items]
        r60 = [r["ret60"] for r in items]
        return {
            "n": len(items),
            "up20": _acc([r["up20"] for r in items]),
            "up60": _acc([r["up60"] for r in items]),
            "mean20": float(np.mean(r20)),
            "mean60": float(np.mean(r60)),
        }

    by_market = {name: pack(subset(lambda r, n=name: r["market"] == n)) for name in ("bull", "chop", "bear")}
    # Stock 60d up when index is bull vs not, further split by stock structure.
    cross = {}
    for mkt_name in ("bull", "chop", "bear"):
        for stk in ("bull", "chop", "bear"):
            cross[f"{mkt_name}|{stk}"] = pack(
                subset(lambda r, m=mkt_name, s=stk: r["market"] == m and r["stock"] == s)
            )
    by_action = {name: pack(subset(lambda r, n=name: r["action"] == n)) for name in ("wait", "buy_dip", "hold", "sell_rally", "reduce")}

    # Long-only process: capture 20d return when we buy_dip / hold; 0 when wait/sell/reduce.
    # "Correct sell" = 20d later the name was down (we avoided the drop).
    deployed = subset(lambda r: r["action"] in ("buy_dip", "hold"))
    sold = subset(lambda r: r["action"] in ("sell_rally", "reduce"))
    waiting = subset(lambda r: r["action"] == "wait")
    process = {
        "deployed": pack(deployed),
        "sold_then_down20": _acc([not r["up20"] for r in sold]) if sold else None,
        "sold_n": len(sold),
        "wait_then_down60": _acc([not r["up60"] for r in waiting]) if waiting else None,
        "wait_n": len(waiting),
        "all": pack(rows),
        "buy_and_hold60_up": _acc([r["up60"] for r in rows]),
        "buy_and_hold20_mean": float(np.mean([r["ret20"] for r in rows])) if rows else None,
        "deployed20_mean": float(np.mean([r["ret20"] for r in deployed])) if deployed else None,
    }

    # Chop-only oscillation: buy_dip vs sell_rally.
    chop_buy = subset(lambda r: r["market"] == "chop" and r["action"] == "buy_dip")
    chop_sell = subset(lambda r: r["market"] == "chop" and r["action"] == "sell_rally")
    chop_stats = {
        "buy_dip": pack(chop_buy),
        "sell_rally": pack(chop_sell),
        "sell_then_down20": _acc([not r["up20"] for r in chop_sell]) if chop_sell else None,
    }

    live_counts = defaultdict(int)
    for row in live:
        live_counts[row["action"]] += 1

    payload = {
        "n_rows": len(rows),
        "n_live": len(live),
        "by_market": by_market,
        "cross": cross,
        "by_action": by_action,
        "process": process,
        "chop": chop_stats,
        "live_counts": dict(live_counts),
        "live": live,
        "market_live": None,
    }
    last_mkt = classify_structure(mkt_close)
    if last_mkt is not None:
        payload["market_live"] = {
            "regime": last_mkt.regime,
            "mom120": last_mkt.mom120,
            "as_of": str(pd.Timestamp(mkt.index[-1]).date()),
        }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH} n={len(rows)}")
    print("market", json.dumps(by_market, indent=2))
    print("action", json.dumps(by_action, indent=2))
    print("process", json.dumps(process, indent=2))
    print("chop", json.dumps(chop_stats, indent=2))
    print("live_market", payload["market_live"])
    print("live_counts", dict(live_counts))


if __name__ == "__main__":
    main()
