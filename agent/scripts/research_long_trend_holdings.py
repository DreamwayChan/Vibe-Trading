"""Walk-forward long-horizon direction test on current monitor holdings.

Question
--------
Can a K-line trend algorithm call the *major* future direction (not next-bar
price) with ~80% accuracy on this book's names?

Literature-backed methods (not next-day classifiers):
- Time-series momentum (Moskowitz / Ooi / Pedersen 2012): sign of 60/120/240d
  return predicts the next 1–12 months.
- Multi-MA stack + ADX filter: only call when 20/60/120 are aligned and ADX
  says the tape is trending (classic trend-following filter).
- High-conviction ensemble: MA + ADX + TSMOM must agree, else abstain.

Evaluation is causal: at each decision bar only past data is used; labels are
the *realized* forward 40/60/120-session close-to-close sign. 80% is tested
on high-conviction calls, not on every bar.

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

OUT_PATH = Path.home() / ".vibe-trading" / "cache" / "long_trend_holdings_result.json"
START_DATE = "2018-01-01"
END_DATE = "2026-08-21"
MARKET = "000300.SH"
HORIZONS = (40, 60, 120)
STEP = 20
ADX_PERIOD = 14
MIN_BARS = 260
MATERIAL_RET = 0.08  # 8% move counts as a "major" directional outcome

BOARD_LABELS = {
    "20260725_184000_05_F0_drug_129": "创新药",
    "20260725_184000_05_F0_robot_124_178": "人形机器人",
    "20260725_193311_05_F0_aiapp_171": "AI应用",
    "20260726_084500_03_F0_machine_171_054": "工业母机",
    "20260726_113500_01_ETF_F0": "ETF",
    "20260726_123500_03_F0_vrar_077": "虚拟现实",
    "20260726_131500_03_F0_copper_081_160": "金属铜",
    "20260729_135623_02_F1_broker": "券商",
    "20260730_162800_03_F0_autoparts_100_081": "汽车零部件",
    "20260730_162800_03_F0_foodbev_127": "食品饮料",
    "20260730_162800_03_F0_speceq_127_111": "专用设备",
    "20260731_tech3_05_F0_memory_185_070": "存储芯片",
}


def load_holdings() -> list[dict]:
    """Latest non-flat holdings from every monitor board."""
    boards = Path.home() / ".vibe-trading" / "monitor" / "boards"
    rows: list[dict] = []
    seen: set[str] = set()
    for path in sorted(boards.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        run_id = str((raw.get("strategy") or {}).get("run_id") or "")
        board = BOARD_LABELS.get(run_id, run_id or path.stem)
        for h in raw.get("holdings") or []:
            symbol = str(h.get("symbol") or "").strip().upper()
            weight = float(h.get("weight") or 0.0)
            action = str(h.get("action") or "").strip().lower()
            if not symbol or abs(weight) < 1e-9 or action == "exit":
                continue
            if symbol in seen:
                continue
            seen.add(symbol)
            rows.append({"symbol": symbol, "board": board, "weight": weight, "action": action})
    rows.sort(key=lambda r: (r["board"], r["symbol"]))
    return rows


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (RMA): first value is SMA, then recursive."""
    arr = series.to_numpy(dtype=float)
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < period:
        return pd.Series(out, index=series.index)
    seed = np.nanmean(arr[:period])
    out[period - 1] = seed
    alpha = 1.0 / period
    for i in range(period, len(arr)):
        prev = out[i - 1]
        x = arr[i]
        if np.isnan(x) or np.isnan(prev):
            out[i] = np.nan
        else:
            out[i] = prev + alpha * (x - prev)
    return pd.Series(out, index=series.index)


def adx_di(frame: pd.DataFrame, period: int = ADX_PERIOD) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder ADX / +DI / -DI on OHLC."""
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    up = high - prev_high
    down = prev_low - low
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm_s = _wilder(pd.Series(plus_dm, index=frame.index), period)
    minus_dm_s = _wilder(pd.Series(minus_dm, index=frame.index), period)
    tr_s = _wilder(tr, period)
    plus_di = 100.0 * plus_dm_s / tr_s.replace(0, np.nan)
    minus_di = 100.0 * minus_dm_s / tr_s.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder(dx, period)
    return adx, plus_di, minus_di


def _sign(x: float) -> int:
    if np.isnan(x) or x == 0:
        return 0
    return 1 if x > 0 else -1


def precompute(frame: pd.DataFrame) -> pd.DataFrame:
    """Vectorized causal features for every bar (no look-ahead)."""
    close = frame["close"].astype(float)
    out = pd.DataFrame(index=frame.index)
    out["close"] = close
    out["ma20"] = close.rolling(20, min_periods=20).mean()
    out["ma60"] = close.rolling(60, min_periods=60).mean()
    out["ma120"] = close.rolling(120, min_periods=120).mean()
    out["r60"] = close / close.shift(60) - 1.0
    out["r120"] = close / close.shift(120) - 1.0
    out["r240"] = close / close.shift(240) - 1.0
    adx, plus_di, minus_di = adx_di(frame)
    out["adx"] = adx
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di
    ma_up = (out["ma20"] > out["ma60"]) & (out["ma60"] > out["ma120"]) & (close > out["ma60"])
    ma_dn = (out["ma20"] < out["ma60"]) & (out["ma60"] < out["ma120"]) & (close < out["ma60"])
    out["ma_dir"] = np.where(ma_up, 1, np.where(ma_dn, -1, 0)).astype(int)
    adx_ok = out["adx"] >= 25
    out["adx_dir"] = np.where(
        adx_ok & (out["plus_di"] > out["minus_di"]),
        1,
        np.where(adx_ok & (out["plus_di"] < out["minus_di"]), -1, 0),
    ).astype(int)
    out["adx_strong"] = (out["adx"] >= 30).fillna(False)
    out["tsmom60"] = np.sign(out["r60"].fillna(0)).astype(int)
    out["tsmom120"] = np.sign(out["r120"].fillna(0)).astype(int)
    out["tsmom240"] = np.sign(out["r240"].fillna(0)).astype(int)
    out.loc[out["r60"].isna(), "tsmom60"] = 0
    out.loc[out["r120"].isna(), "tsmom120"] = 0
    out.loc[out["r240"].isna(), "tsmom240"] = 0
    agree = (
        (out["ma_dir"] != 0)
        & (out["adx_dir"] == out["ma_dir"])
        & (out["tsmom120"] == out["ma_dir"])
    )
    out["ensemble"] = np.where(agree, out["ma_dir"], 0).astype(int)
    strong = agree & out["adx_strong"] & (out["tsmom60"] == out["ma_dir"])
    out["ensemble_hi"] = np.where(strong, out["ma_dir"], 0).astype(int)
    return out


def features_at(feat: pd.DataFrame, i: int) -> dict | None:
    """Causal snapshot at bar i (inclusive). Returns None if lookback is thin."""
    if i < MIN_BARS or i >= len(feat):
        return None
    row = feat.iloc[i]
    if not np.isfinite(row["close"]) or float(row["close"]) <= 0:
        return None
    if not np.isfinite(row["ma120"]):
        return None
    return {
        "close": float(row["close"]),
        "ma_dir": int(row["ma_dir"]),
        "adx": float(row["adx"]) if np.isfinite(row["adx"]) else float("nan"),
        "adx_dir": int(row["adx_dir"]),
        "adx_strong": bool(row["adx_strong"]),
        "tsmom60": int(row["tsmom60"]),
        "tsmom120": int(row["tsmom120"]),
        "tsmom240": int(row["tsmom240"]),
        "ensemble": int(row["ensemble"]),
        "ensemble_hi": int(row["ensemble_hi"]),
        "r60": float(row["r60"]) if np.isfinite(row["r60"]) else float("nan"),
        "r120": float(row["r120"]) if np.isfinite(row["r120"]) else float("nan"),
        "r240": float(row["r240"]) if np.isfinite(row["r240"]) else float("nan"),
    }


def forward_labels(close: pd.Series, i: int, horizon: int) -> dict | None:
    """Realized forward path stats from bar i (exclusive of i, inclusive of i+h)."""
    if i + horizon >= len(close):
        return None
    start = float(close.iloc[i])
    if start <= 0:
        return None
    future = close.iloc[i + 1 : i + 1 + horizon].astype(float)
    end = float(future.iloc[-1])
    ret = end / start - 1.0
    above = float((future > start).mean())
    max_up = float(future.max() / start - 1.0)
    max_dn = float(future.min() / start - 1.0)
    return {
        "ret": ret,
        "sign": _sign(ret),
        "above": above,
        "max_up": max_up,
        "max_dn": max_dn,
        "material": abs(ret) >= MATERIAL_RET,
    }


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
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                break
        else:
            out[str(code).upper()] = df
    return out


def score_calls(rows: list[dict], method: str, horizon: int, *, overlap: bool) -> dict:
    """Accuracy stats for one method/horizon. overlap=False uses step=horizon."""
    step = STEP if overlap else horizon
    picked = [r for r in rows if r["horizon"] == horizon and r.get(method, 0) != 0]
    # Dedup by (symbol, i) already unique; subsample for non-overlap.
    if not overlap:
        by_sym: dict[str, list[dict]] = defaultdict(list)
        for r in picked:
            by_sym[r["symbol"]].append(r)
        picked = []
        for items in by_sym.values():
            items.sort(key=lambda x: x["i"])
            last_i = -10_000
            for r in items:
                if r["i"] - last_i >= step:
                    picked.append(r)
                    last_i = r["i"]
    n = len(picked)
    if n == 0:
        return {"n": 0, "acc": None, "acc_material": None, "path_acc": None, "coverage": 0.0}
    hits = sum(1 for r in picked if r[method] == r["fwd_sign"] and r["fwd_sign"] != 0)
    usable = sum(1 for r in picked if r["fwd_sign"] != 0)
    mat = [r for r in picked if r["material"] and r["fwd_sign"] != 0]
    mat_hits = sum(1 for r in mat if r[method] == r["fwd_sign"])
    path_hits = 0
    for r in picked:
        pred = r[method]
        above = r["above"]
        if pred == 1 and above >= 0.5:
            path_hits += 1
        elif pred == -1 and above <= 0.5:
            path_hits += 1
    total_pool = sum(1 for r in rows if r["horizon"] == horizon)
    return {
        "n": n,
        "usable": usable,
        "acc": (hits / usable) if usable else None,
        "acc_material": (mat_hits / len(mat)) if mat else None,
        "n_material": len(mat),
        "path_acc": path_hits / n if n else None,
        "coverage": n / total_pool if total_pool else 0.0,
        "up_calls": sum(1 for r in picked if r[method] == 1),
        "dn_calls": sum(1 for r in picked if r[method] == -1),
        "up_acc": _acc_side(picked, method, 1),
        "dn_acc": _acc_side(picked, method, -1),
    }


def _acc_side(picked: list[dict], method: str, side: int) -> float | None:
    subset = [r for r in picked if r[method] == side and r["fwd_sign"] != 0]
    if not subset:
        return None
    hits = sum(1 for r in subset if r["fwd_sign"] == side)
    return hits / len(subset)


def baseline_always_up(rows: list[dict], horizon: int) -> dict:
    pool = [r for r in rows if r["horizon"] == horizon and r["fwd_sign"] != 0]
    if not pool:
        return {"n": 0, "acc": None}
    hits = sum(1 for r in pool if r["fwd_sign"] == 1)
    return {"n": len(pool), "acc": hits / len(pool)}


def main() -> None:
    holdings = load_holdings()
    symbols = [h["symbol"] for h in holdings]
    print(f"holdings={len(symbols)} boards={len({h['board'] for h in holdings})}")
    data = fetch_all(symbols)
    missing = [s for s in symbols if s not in data]
    print(f"fetched={len(data)} missing={missing}")

    mkt = data.get(MARKET)
    mkt_feat: dict[pd.Timestamp, dict] = {}
    if mkt is not None:
        mkt_pre = precompute(mkt)
        mkt_idx = list(mkt.index)
        for i in range(MIN_BARS, len(mkt), STEP):
            feat = features_at(mkt_pre, i)
            if feat:
                mkt_feat[pd.Timestamp(mkt_idx[i])] = feat

    records: list[dict] = []
    live: list[dict] = []
    board_of = {h["symbol"]: h["board"] for h in holdings}

    for symbol, frame in data.items():
        if symbol == MARKET:
            continue
        if symbol not in board_of:
            continue
        close = frame["close"].astype(float)
        n = len(frame)
        feat_frame = precompute(frame)
        # Live snapshot: last bar with enough lookback.
        last_i = n - 1
        live_feat = features_at(feat_frame, last_i)
        if live_feat:
            live.append(
                {
                    "symbol": symbol,
                    "board": board_of[symbol],
                    "as_of": str(pd.Timestamp(frame.index[last_i]).date()),
                    **{k: live_feat[k] for k in ("ma_dir", "adx", "adx_dir", "tsmom120", "ensemble", "ensemble_hi", "r120")},
                }
            )
        for i in range(MIN_BARS, n, STEP):
            feat = features_at(feat_frame, i)
            if feat is None:
                continue
            # Market overlay: same-day CSI 300 MA direction if available.
            ts = pd.Timestamp(frame.index[i])
            mkt_dir = 0
            nearest = min(mkt_feat.keys(), key=lambda t: abs((t - ts).days), default=None) if mkt_feat else None
            if nearest is not None and abs((nearest - ts).days) <= 3:
                mkt_dir = int(mkt_feat[nearest]["ma_dir"])
            aligned = feat["ensemble"] if feat["ensemble"] != 0 and (mkt_dir == 0 or mkt_dir == feat["ensemble"]) else 0
            for h in HORIZONS:
                lab = forward_labels(close, i, h)
                if lab is None:
                    continue
                records.append(
                    {
                        "symbol": symbol,
                        "board": board_of[symbol],
                        "i": i,
                        "date": str(ts.date()),
                        "horizon": h,
                        "ma_dir": feat["ma_dir"],
                        "adx_dir": feat["adx_dir"],
                        "tsmom60": feat["tsmom60"],
                        "tsmom120": feat["tsmom120"],
                        "tsmom240": feat["tsmom240"],
                        "ensemble": feat["ensemble"],
                        "ensemble_hi": feat["ensemble_hi"],
                        "mkt_aligned": aligned,
                        "fwd_sign": lab["sign"],
                        "fwd_ret": lab["ret"],
                        "above": lab["above"],
                        "material": lab["material"],
                    }
                )

    methods = [
        "tsmom60",
        "tsmom120",
        "tsmom240",
        "ma_dir",
        "adx_dir",
        "ensemble",
        "ensemble_hi",
        "mkt_aligned",
    ]
    summary: dict[str, dict] = {}
    for method in methods:
        summary[method] = {}
        for h in HORIZONS:
            summary[method][str(h)] = {
                "overlap20": score_calls(records, method, h, overlap=True),
                "nonoverlap": score_calls(records, method, h, overlap=False),
            }

    always = {str(h): baseline_always_up(records, h) for h in HORIZONS}

    by_board: dict[str, dict] = {}
    for board in sorted({h["board"] for h in holdings}):
        subset = [r for r in records if r["board"] == board]
        by_board[board] = {
            "n_names": sum(1 for h in holdings if h["board"] == board),
            "ensemble_60": score_calls(subset, "ensemble", 60, overlap=True),
            "ensemble_hi_60": score_calls(subset, "ensemble_hi", 60, overlap=True),
            "tsmom120_60": score_calls(subset, "tsmom120", 60, overlap=True),
            "always_up_60": baseline_always_up(subset, 60),
        }

    per_name = []
    for symbol in symbols:
        subset = [r for r in records if r["symbol"] == symbol]
        per_name.append(
            {
                "symbol": symbol,
                "board": board_of[symbol],
                "ensemble_60": score_calls(subset, "ensemble", 60, overlap=True),
                "ensemble_hi_60": score_calls(subset, "ensemble_hi", 60, overlap=True),
                "tsmom120_60": score_calls(subset, "tsmom120", 60, overlap=True),
            }
        )

    payload = {
        "as_of": END_DATE,
        "start": START_DATE,
        "n_holdings": len(holdings),
        "n_fetched": len([s for s in symbols if s in data]),
        "missing": missing,
        "n_records": len(records),
        "material_ret": MATERIAL_RET,
        "methods": summary,
        "always_up": always,
        "by_board": by_board,
        "per_name": per_name,
        "live": live,
        "holdings": holdings,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    for method in ("ensemble_hi", "ensemble", "ma_dir", "tsmom120", "adx_dir"):
        row = summary[method]["60"]["overlap20"]
        acc = row["acc"]
        acc_s = f"{acc:.1%}" if acc is not None else "n/a"
        print(
            f"{method:14s} n={row['n']:4d} cov={row['coverage']:.1%} "
            f"acc={acc_s} mat={row['acc_material']} path={row['path_acc']}"
        )


if __name__ == "__main__":
    main()
