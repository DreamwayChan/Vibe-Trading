"""Screen A-share industry/concept boards: momentum + fundamentals → Top 5.

Scoring dimensions (equal-weight ranks → composite):
  - Trend / momentum: 20D / 60D / 120D returns, MA20>MA60, recent amount strength
  - Fundamentals (median across sample constituents): ROE, revenue YoY, PE quality
  - Prospect tilt: prefer boards with positive multi-horizon trend + non-broken ROE

Research only — not investment advice.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

OUT_JSON = _AGENT_ROOT / "runs" / "_tmp_sector_screen" / "sector_top5.json"
OUT_CSV = _AGENT_ROOT / "runs" / "_tmp_sector_screen" / "sector_screen_full.csv"

# Soft exclude pure-hype / ST / narrow event boards from "prospect" ranking.
_EXCLUDE_NAME_SUBSTR = (
    "ST",
    "退市",
    "次新",
    "预盈预增",
    "昨日连板",
    "昨日涨停",
    "高送转",
    "股权转让",
    "举牌",
    "微盘股",
    "破净股",
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _as_float(v: Any) -> float | None:
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_board_list(board_type: str, limit: int = 200) -> list[dict]:
    """Fetch Eastmoney board list. board_type: industry|concept."""
    fs = "m:90+t:2" if board_type == "industry" else "m:90+t:3"
    s = _session()
    rows: list[dict] = []
    for pn in range(1, 6):
        params = dict(
            pn=pn,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f62",  # main net inflow proxy for liquidity attention
            fs=fs,
            fields="f12,f14,f2,f3,f62,f66,f69,f72,f75,f78,f81,f84,f87,f104,f105,f124,f128,f136,f184",
        )
        for url in (
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            "https://push2.eastmoney.com/api/qt/clist/get",
        ):
            try:
                r = s.get(url, params=params, timeout=30)
                r.raise_for_status()
                data = r.json().get("data") or {}
                diff = data.get("diff") or []
                break
            except Exception:  # noqa: BLE001
                diff = []
                data = {}
        if not diff:
            break
        for x in diff:
            code = str(x.get("f12") or "")
            name = str(x.get("f14") or "")
            if not code or not name:
                continue
            if any(k in name for k in _EXCLUDE_NAME_SUBSTR):
                continue
            rows.append(
                {
                    "bk": code,
                    "name": name,
                    "board_type": board_type,
                    "chg_1d": _as_float(x.get("f3")),
                    "index": _as_float(x.get("f2")),
                    "net_inflow": _as_float(x.get("f62")),
                    "up": x.get("f104"),
                    "down": x.get("f105"),
                    "leader": x.get("f128"),
                }
            )
        total = data.get("total") or 0
        if len(rows) >= min(limit, total or limit):
            break
        time.sleep(0.15)
    # Dedup keep first (already sorted by fid)
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r["bk"] in seen:
            continue
        seen.add(r["bk"])
        out.append(r)
        if len(out) >= limit:
            break
    return out


def fetch_board_kline(bk: str, limit: int = 160) -> pd.DataFrame:
    """Daily OHLCV for board BK via Eastmoney kline (secid=90.BKxxxx)."""
    s = _session()
    params = dict(
        secid=f"90.{bk}",
        fields1="f1,f2,f3,f4,f5,f6",
        fields2="f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        klt=101,
        fqt=1,
        end="20500101",
        lmt=limit,
        ut="fa5fd1943c7b386f172d6893dbfba10b",
    )
    last_err = None
    for url in (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
    ):
        try:
            r = s.get(url, params=params, timeout=30)
            r.raise_for_status()
            kl = ((r.json().get("data") or {}).get("klines")) or []
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            kl = []
    if not kl:
        if last_err:
            print(f"  [kline miss] {bk}: {last_err}")
        return pd.DataFrame()
    rows = []
    for line in kl:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "vol": float(parts[5]),
                "amount": float(parts[6]),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def momentum_metrics(df: pd.DataFrame) -> dict[str, float | None]:
    if df is None or len(df) < 30:
        return {}
    close = df["close"].astype(float)
    amount = df["amount"].astype(float)

    def ret(n: int) -> float | None:
        if len(close) <= n:
            return None
        c0, c1 = float(close.iloc[-1]), float(close.iloc[-(n + 1)])
        if c1 == 0:
            return None
        return (c0 / c1 - 1.0) * 100.0

    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float("nan")
    amt_20 = float(amount.tail(20).mean()) if len(amount) >= 20 else float("nan")
    amt_60 = float(amount.tail(60).mean()) if len(amount) >= 60 else float("nan")
    amt_ratio = (amt_20 / amt_60) if amt_60 and not math.isnan(amt_60) and amt_60 > 0 else None

    # Max drawdown over last 60 sessions (path quality)
    win = close.tail(60)
    peak = win.cummax()
    dd = float(((win / peak) - 1.0).min() * 100.0) if len(win) else None

    # Consistency: share of up days in last 20
    chg = close.pct_change()
    up_share_20 = float((chg.tail(20) > 0).mean()) if len(chg) >= 21 else None

    return {
        "ret_5d": ret(5),
        "ret_20d": ret(20),
        "ret_60d": ret(60),
        "ret_120d": ret(120),
        "ma20": ma20,
        "ma60": ma60 if not math.isnan(ma60) else None,
        "trend_ok": 1.0 if (not math.isnan(ma60) and ma20 > ma60 and close.iloc[-1] > ma20) else 0.0,
        "amt_ratio_20_60": amt_ratio,
        "amt_20d_yi": amt_20 / 1e8 if not math.isnan(amt_20) else None,
        "dd_60d": dd,
        "up_share_20": up_share_20,
        "last_close": float(close.iloc[-1]),
        "last_date": str(df["date"].iloc[-1].date()),
    }


def fetch_constituents(bk: str, max_n: int = 40) -> list[str]:
    s = _session()
    codes: list[str] = []
    for pn in range(1, 3):
        params = dict(
            pn=pn,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f3",
            fs=f"b:{bk}",
            fields="f12,f13,f14,f3,f20",
        )
        try:
            r = s.get(
                "https://push2delay.eastmoney.com/api/qt/clist/get",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            diff = ((r.json().get("data") or {}).get("diff")) or []
        except Exception:  # noqa: BLE001
            diff = []
        if not diff:
            break
        for x in diff:
            c = str(x.get("f12") or "").zfill(6)
            mkt = int(x.get("f13") or 0)
            if mkt == 1:
                ts = f"{c}.SH"
            elif c.startswith(("4", "8", "92")):
                continue  # drop BJ
            else:
                ts = f"{c}.SZ"
            codes.append(ts)
        if len(codes) >= max_n:
            break
        time.sleep(0.1)
    return codes[:max_n]


def fundamentals_for_codes(codes: list[str]) -> dict[str, float | None]:
    """Median ROE / revenue YoY / PE across sample; requires Tushare."""
    if not codes:
        return {}
    try:
        from backtest.loaders.tushare_fundamentals import TushareFundamentalProvider
        from backtest.loaders.tushare_pool import PoolApiProxy, get_tushare_pool
    except Exception as exc:  # noqa: BLE001
        print(f"  [fundamentals import fail] {exc}")
        return {}

    sample = codes[:25]
    roe_vals: list[float] = []
    yoy_vals: list[float] = []
    pe_vals: list[float] = []
    try:
        prov = TushareFundamentalProvider()
        fina = prov.query_fundamentals(
            "fina_indicator",
            sample,
            as_of="2026-07-28",
            fields=["roe", "tr_yoy", "netprofit_yoy", "eps"],
        )
        # fina may be MultiIndex / long table — normalize
        if fina is not None and not getattr(fina, "empty", True):
            df = fina.reset_index() if hasattr(fina, "reset_index") else pd.DataFrame(fina)
            # expect columns like ts_code / code + roe
            code_col = next((c for c in df.columns if str(c).lower() in ("ts_code", "code", "symbol")), None)
            for col, bucket in (("roe", roe_vals), ("tr_yoy", yoy_vals), ("netprofit_yoy", yoy_vals)):
                if col not in df.columns:
                    continue
                for v in df[col].tolist():
                    fv = _as_float(v)
                    if fv is not None and abs(fv) < 500:
                        bucket.append(fv)
    except Exception as exc:  # noqa: BLE001
        print(f"  [fina fail] {exc}")

    try:
        pro = PoolApiProxy(get_tushare_pool())
        # latest daily_basic PE for sample (batch)
        # trade_date omit → use per-code latest via daily_basic with codes join
        frames = []
        for i in range(0, len(sample), 20):
            chunk = sample[i : i + 20]
            try:
                db = pro.daily_basic(
                    ts_code=",".join(chunk),
                    trade_date="20260728",
                    fields="ts_code,pe_ttm,pb,total_mv",
                )
                if db is not None and not db.empty:
                    frames.append(db)
            except Exception:  # noqa: BLE001
                # fallback: no date filter — some tokens need trade_date
                pass
            time.sleep(0.05)
        if not frames:
            # try recent calendar dates
            for d in ("20260725", "20260724", "20260723", "20260722"):
                try:
                    db = pro.daily_basic(
                        trade_date=d,
                        fields="ts_code,pe_ttm,pb,total_mv",
                    )
                    if db is not None and not db.empty:
                        db = db[db["ts_code"].isin(sample)]
                        if not db.empty:
                            frames.append(db)
                            break
                except Exception:  # noqa: BLE001
                    continue
        if frames:
            pe_df = pd.concat(frames, ignore_index=True)
            for v in pe_df.get("pe_ttm", pd.Series(dtype=float)).tolist():
                fv = _as_float(v)
                if fv is not None and 0 < fv < 200:
                    pe_vals.append(fv)
    except Exception as exc:  # noqa: BLE001
        print(f"  [pe fail] {exc}")

    def med(xs: list[float]) -> float | None:
        return float(np.median(xs)) if xs else None

    return {
        "roe_med": med(roe_vals),
        "rev_yoy_med": med(yoy_vals),
        "pe_ttm_med": med(pe_vals),
        "fund_n": float(len(sample)),
        "roe_n": float(len(roe_vals)),
        "pe_n": float(len(pe_vals)),
    }


def rank_pct(series: pd.Series, ascending: bool = False) -> pd.Series:
    return series.rank(pct=True, ascending=ascending, method="average")


def main() -> int:
    print("=== Phase 1: list boards ===")
    industry = fetch_board_list("industry", limit=120)
    concept = fetch_board_list("concept", limit=180)
    boards = industry + concept
    print(f"boards: industry={len(industry)} concept={len(concept)} total={len(boards)}")

    print("=== Phase 2: kline momentum (liquidity prefilter) ===")
    # Prefer boards with non-null net_inflow magnitude / listed first
    scored_rows: list[dict] = []
    for i, b in enumerate(boards):
        bk = b["bk"]
        print(f"[{i+1}/{len(boards)}] {bk} {b['name']}")
        kdf = fetch_board_kline(bk, limit=160)
        mm = momentum_metrics(kdf)
        if not mm or mm.get("ret_20d") is None:
            time.sleep(0.08)
            continue
        row = {**b, **mm}
        scored_rows.append(row)
        time.sleep(0.08)

    mom = pd.DataFrame(scored_rows)
    if mom.empty:
        print("No momentum data — abort")
        return 1

    # Liquidity filter: need meaningful amount
    mom = mom[mom["amt_20d_yi"].fillna(0) > 5].copy()  # >5亿日均成交额
    print(f"after liquidity filter: {len(mom)}")

    # Momentum composite (higher better). dd_60d is negative; closer to 0 ranks higher.
    mom["mom_score"] = (
        0.20 * rank_pct(mom["ret_5d"].fillna(-999))
        + 0.25 * rank_pct(mom["ret_20d"].fillna(-999))
        + 0.25 * rank_pct(mom["ret_60d"].fillna(-999))
        + 0.15 * rank_pct(mom["ret_120d"].fillna(-999))
        + 0.10 * mom["trend_ok"].fillna(0)
        + 0.05 * rank_pct(mom["amt_ratio_20_60"].fillna(0))
        + 0.05 * rank_pct(mom["up_share_20"].fillna(0))
        + 0.10 * rank_pct(mom["dd_60d"].fillna(-999))
    ) / 1.15

    # Require positive medium-term momentum for "优秀走势"
    candidates = mom[
        (mom["ret_20d"].fillna(-999) > 0)
        & (mom["ret_60d"].fillna(-999) > -5)
        & (mom["trend_ok"].fillna(0) >= 0.5)
    ].copy()
    candidates = candidates.sort_values("mom_score", ascending=False).head(25)
    print(f"momentum shortlist: {len(candidates)}")
    print(candidates[["bk", "name", "board_type", "ret_20d", "ret_60d", "mom_score"]].to_string(index=False))

    print("=== Phase 3: fundamentals on shortlist ===")
    fund_rows = []
    for _, r in candidates.iterrows():
        bk = r["bk"]
        print(f" fundamentals {bk} {r['name']}")
        codes = fetch_constituents(bk, max_n=30)
        f = fundamentals_for_codes(codes)
        fund_rows.append({"bk": bk, "n_members": len(codes), **f})
        time.sleep(0.2)

    fund_df = pd.DataFrame(fund_rows)
    merged = candidates.merge(fund_df, on="bk", how="left")

    # Fundamental quality gates (soft): ROE not broken; PE not absurd if available
    def fund_ok(row: pd.Series) -> bool:
        roe = row.get("roe_med")
        pe = row.get("pe_ttm_med")
        yoy = row.get("rev_yoy_med")
        if roe is not None and not (isinstance(roe, float) and math.isnan(roe)):
            if roe < 3:  # median ROE too weak
                return False
        if pe is not None and not (isinstance(pe, float) and math.isnan(pe)):
            if pe > 120:
                return False
        # allow missing yoy; if present require not collapse
        if yoy is not None and not (isinstance(yoy, float) and math.isnan(yoy)):
            if yoy < -30:
                return False
        return True

    merged["fund_ok"] = merged.apply(fund_ok, axis=1)
    # Prefer fund_ok; if too few, keep top mom with note
    viable = merged[merged["fund_ok"]].copy()
    if len(viable) < 5:
        print(f"fund_ok only {len(viable)}; relaxing to top momentum with available fund metrics")
        viable = merged.copy()

    # Fundamental score
    viable["fund_score"] = (
        0.40 * rank_pct(viable["roe_med"].fillna(0))
        + 0.35 * rank_pct(viable["rev_yoy_med"].fillna(0))
        + 0.25 * rank_pct(viable["pe_ttm_med"].fillna(999), ascending=True)  # lower PE better
    )

    # Prospect: blend momentum + fundamentals; slight industry preference for stability
    viable["type_bonus"] = viable["board_type"].map({"industry": 0.05, "concept": 0.0}).fillna(0)
    viable["composite"] = 0.55 * viable["mom_score"] + 0.40 * viable["fund_score"] + viable["type_bonus"]

    # Diversify: if two near-duplicate names, keep higher composite
    viable = viable.sort_values("composite", ascending=False)
    top = viable.head(12).copy()

    # De-overlap by name similarity (simple)
    final: list[dict] = []
    used_names: list[str] = []
    for _, row in top.iterrows():
        name = str(row["name"])
        if any(name in u or u in name for u in used_names):
            continue
        used_names.append(name)
        final.append(row.to_dict())
        if len(final) >= 5:
            break

    # If still short, fill from viable
    if len(final) < 5:
        for _, row in viable.iterrows():
            if row["bk"] in {x["bk"] for x in final}:
                continue
            final.append(row.to_dict())
            if len(final) >= 5:
                break

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    mom.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    payload = {
        "as_of": str(pd.Timestamp.today().date()),
        "method": {
            "momentum": "Eastmoney board kline 5/20/60/120D + MA trend + amount ratio",
            "fundamentals": "Tushare fina_indicator median ROE/tr_yoy + daily_basic PE",
            "composite": "0.55*mom + 0.40*fund + industry bonus",
            "universe": f"industry≈{len(industry)} + concept≈{len(concept)}, liquidity amt_20d>5亿",
        },
        "top5": final,
        "shortlist": viable.head(15).to_dict(orient="records"),
    }
    # numpy types → native
    def _clean(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            if math.isnan(float(o)) or math.isinf(float(o)):
                return None
            return float(o)
        if isinstance(o, (np.integer, int)):
            return int(o)
        if isinstance(o, (np.bool_, bool)):
            return bool(o)
        if pd.isna(o):
            return None
        return o

    OUT_JSON.write_text(json.dumps(_clean(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== TOP 5 ===")
    for i, r in enumerate(final, 1):
        print(
            f"{i}. {r['name']} ({r['bk']}/{r['board_type']}) "
            f"composite={r.get('composite'):.3f} "
            f"ret20={r.get('ret_20d')} ret60={r.get('ret_60d')} "
            f"roe={r.get('roe_med')} yoy={r.get('rev_yoy_med')} pe={r.get('pe_ttm_med')}"
        )
    print(f"wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
