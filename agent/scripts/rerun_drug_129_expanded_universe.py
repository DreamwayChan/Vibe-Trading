"""Expand 创新药 F0' universe (BK1106 ∪ existing) and re-run 164+129.

Why this exists:
  The formal run ``20260725_184000_05_F0_drug_129`` kept a ~97-name panel
  copied from an older OHLCV cache. Eastmoney concept BK1106 (创新药) now
  lists ~190 names, so many liquid names were absent from the cross-section.
  We union the live board with the prior panel (keeps names like 300142.SZ
  even if they temporarily drop off the board), refresh full-horizon OHLCV,
  and overwrite the formal F0' artifacts in place.

Run from agent/:
    python scripts/rerun_drug_129_expanded_universe.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

# Formal scripts often run outside `vibe-trading serve`; load agent/.env so
# TUSHARE_TOKEN and loader flags are visible to DataLoader.
try:
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import (  # noqa: E402
    _AutoLoader,
    _create_market_engine,
    _load_module_from_file,
)

BK = "BK1106"
BK_NAME = "创新药"
START_DATE = "2024-01-01"
END_DATE = "2026-07-24"
MIN_BARS = 120
MC_SIMS = 1000
INITIAL_CASH = 10_000_000.0

F0_RUN = _AGENT_ROOT / "runs" / "20260725_184000_05_F0_drug_129"
UNIV_RUN = _AGENT_ROOT / "runs" / "20260807_drug_00_universe"
PROP_OUT = _AGENT_ROOT / "prop_国泰君安_创新药_最优.md"
COMPARE_OUT = _AGENT_ROOT / "prop_国泰君安_F0_F1_创新药对比.md"


def _is_bj(code: str) -> bool:
    digits = code.split(".")[0]
    return digits.startswith(("4", "8", "92")) or code.upper().endswith(".BJ")


def fetch_bk1106() -> list[dict]:
    """Pull Eastmoney concept BK1106 constituents, market-cap descending."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
    )
    rows: list[dict] = []
    total = None
    for page in range(1, 12):
        params = dict(
            pn=page,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f20",
            fs=f"b:{BK}",
            fields="f12,f13,f14,f20",
        )
        resp = session.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        total = data.get("total", total)
        diff = data.get("diff") or []
        if not diff:
            break
        for item in diff:
            code6 = str(item.get("f12") or "").zfill(6)
            market = int(item.get("f13") or 0)
            if market == 1:
                ts_code = f"{code6}.SH"
            elif code6.startswith(("4", "8", "92")):
                ts_code = f"{code6}.BJ"
            else:
                ts_code = f"{code6}.SZ"
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": str(item.get("f14") or ""),
                    "mv": item.get("f20"),
                    "source": "BK1106",
                }
            )
        if total and len(rows) >= int(total):
            break
        time.sleep(0.08)
    print(f"[universe] BK1106 n={len(rows)} total={total}")
    return rows


def build_universe(prior_codes: list[str], board_rows: list[dict]) -> pd.DataFrame:
    """Union live BK1106 with the prior formal panel; drop BJ; keep 300142."""
    by_code: dict[str, dict] = {}
    for row in board_rows:
        code = str(row["ts_code"]).upper()
        if _is_bj(code):
            continue
        by_code[code] = {
            "ts_code": code,
            "name": row.get("name") or "",
            "mv": row.get("mv"),
            "source": "BK1106",
        }

    # Prior panel names may have left the board (e.g. 300142.SZ); keep them
    # so the re-run does not silently drop names the user asked to include.
    for code in prior_codes:
        code = str(code).upper()
        if _is_bj(code):
            continue
        if code not in by_code:
            by_code[code] = {
                "ts_code": code,
                "name": "",
                "mv": None,
                "source": "prior_panel",
            }

    force_keep = ["300142.SZ"]
    for code in force_keep:
        if code not in by_code:
            by_code[code] = {
                "ts_code": code,
                "name": "",
                "mv": None,
                "source": "forced",
            }

    frame = pd.DataFrame(by_code.values())
    # Prefer board order (already MV-sorted) then prior-only tails.
    frame["_ord"] = range(len(frame))
    frame.loc[frame["source"] != "BK1106", "_ord"] = frame["_ord"] + 10_000
    frame = frame.sort_values("_ord").drop(columns="_ord").reset_index(drop=True)
    return frame


def _normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Align loader frames onto a trade_date index with OHLCV columns."""
    out = frame.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        for col in ("trade_date", "date", "timestamp"):
            if col in out.columns:
                out[col] = pd.to_datetime(out[col])
                out = out.set_index(col)
                break
        else:
            out.index = pd.to_datetime(out.index)
    out = out.sort_index()
    out = out.loc[START_DATE:END_DATE]
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in out.columns]
    for col in keep:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[keep].dropna(subset=["open", "high", "low", "close"])


def _coverage_ok(frame: pd.DataFrame) -> bool:
    """Require enough bars and coverage that reaches near START_DATE."""
    if frame is None or len(frame) < MIN_BARS:
        return False
    first = pd.Timestamp(frame.index.min())
    # Old cache often started ~2024-07 due to a 500-bar fetch cap.
    return first <= pd.Timestamp(START_DATE) + pd.Timedelta(days=10)


def _fetch_one_tushare(code: str):
    from backtest.loaders.tushare import DataLoader

    loader = DataLoader()
    try:
        data = loader.fetch([code], START_DATE, END_DATE, interval="1D")
        frame = data.get(code)
        if frame is None or frame.empty:
            return code, None, "empty"
        return code, _normalize_ohlcv_frame(frame), ""
    except Exception as exc:  # noqa: BLE001
        return code, None, str(exc)


def _fetch_one_akshare(code: str):
    try:
        import akshare as ak
    except ImportError as exc:
        return code, None, f"akshare missing: {exc}"

    symbol = code.split(".")[0]
    try:
        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=START_DATE.replace("-", ""),
            end_date=END_DATE.replace("-", ""),
            adjust="qfq",
        )
    except Exception as exc:  # noqa: BLE001
        return code, None, str(exc)
    if raw is None or raw.empty:
        return code, None, "empty"
    frame = raw.rename(
        columns={
            "日期": "trade_date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
        }
    )
    return code, _normalize_ohlcv_frame(frame), ""


def _fetch_one_eastmoney(code: str):
    from backtest.loaders.eastmoney_loader import DataLoader

    loader = DataLoader()
    try:
        data = loader.fetch([code], START_DATE, END_DATE, interval="1D")
        frame = data.get(code)
        if frame is None or frame.empty:
            return code, None, "empty"
        return code, _normalize_ohlcv_frame(frame), ""
    except Exception as exc:  # noqa: BLE001
        return code, None, str(exc)


def _fetch_one(code: str) -> tuple[str, pd.DataFrame | None, str]:
    """Prefer akshare/eastmoney: tushare is flaky here; tencent caps at 500 bars."""
    errors: list[str] = []
    for fetcher in (_fetch_one_akshare, _fetch_one_eastmoney, _fetch_one_tushare):
        code, frame, err = fetcher(code)
        if frame is not None and _coverage_ok(frame):
            return code, frame, ""
        if err:
            errors.append(f"{fetcher.__name__}:{err}")
    return code, None, " | ".join(errors) or "short/late"


def fetch_ohlcv(codes: list[str], art: Path) -> dict[str, pd.DataFrame]:
    art.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    pending: list[str] = []

    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if path.exists():
            raw = pd.read_csv(path)
            frame = _normalize_ohlcv_frame(raw)
            if _coverage_ok(frame):
                data_map[code] = frame
                continue
        pending.append(code)

    print(f"[fetch] cached_ok={len(data_map)} pending={len(pending)}")
    if not pending:
        return data_map

    t0 = time.time()
    ok = fail = 0
    # Modest concurrency — tushare rate-limits hard under burst traffic.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(_fetch_one, code): code for code in pending}
        for fut in as_completed(futs):
            code, frame, err = fut.result()
            if frame is None or not _coverage_ok(frame):
                fail += 1
                print(f"  skip {code}: {err or 'short/late'}")
                continue
            out = frame.copy()
            out.index.name = "trade_date"
            out.to_csv(art / f"ohlcv_{code}.csv")
            data_map[code] = out
            ok += 1
            if ok % 20 == 0:
                print(f"  progress ok={ok} fail={fail}")
    print(
        f"[fetch] done total={len(data_map)} new_ok={ok} fail={fail} "
        f"{time.time() - t0:.1f}s"
    )
    return data_map


def _seed_cache_from_prior(univ_art: Path, prior_art: Path) -> None:
    """Copy prior OHLCV into the universe dir as a starting cache."""
    univ_art.mkdir(parents=True, exist_ok=True)
    if not prior_art.exists():
        return
    for path in prior_art.glob("ohlcv_*.csv"):
        dst = univ_art / path.name
        if not dst.exists():
            shutil.copy2(path, dst)


def run_backtest(run_dir: Path, data_map: dict[str, pd.DataFrame], codes: list[str]) -> dict:
    """Overwrite formal F0' run artifacts using the existing signal_engine."""
    engine_path = run_dir / "code" / "signal_engine.py"
    if not engine_path.is_file():
        raise SystemExit(f"missing signal engine: {engine_path}")

    art = run_dir / "artifacts"
    # Drop stale OHLCV so the panel matches the expanded codes exactly.
    if art.exists():
        for path in art.glob("ohlcv_*.csv"):
            path.unlink()
    else:
        art.mkdir(parents=True)

    for code in codes:
        out = data_map[code].copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")

    cfg = {
        "codes": codes,
        "source": "tushare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": INITIAL_CASH,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": ["gtja191_164", "gtja191_129"],
        "parameters": {
            "strategy": "confirm_search_129_F0",
            "panel": "创新药 BK1106",
            "confirm_ids": ["gtja191_129"],
            "veto_max": False,
            "universe": (
                f"东财概念「创新药」{BK} ∪ 原面板；排除北交所；"
                f"强制保留 300142.SZ；有效 OHLCV {len(codes)}"
            ),
            "universe_run": UNIV_RUN.name,
        },
        "validation": {"monte_carlo": {"n_simulations": MC_SIMS}},
        "_run_card_effective_sources": ["local_artifacts"],
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    mod = _load_module_from_file(
        engine_path,
        f"eng_drug129_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    engine = _create_market_engine("tushare", cfg, codes)
    bpy = calc_bars_per_year("1D", "tushare")
    print(f"[backtest] symbols={len(codes)} cash={INITIAL_CASH:,.0f} mc={MC_SIMS}")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(art / "metrics.csv").iloc[0].to_dict()
    val_path = art / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {"metrics": metrics, "validation": validation, "n_symbols": len(codes)}


def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x, nd: int = 4) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _holdings_table(run_dir: Path, names: dict[str, str]) -> tuple[str, dict]:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    held = pos.loc[last]
    held = held[held > 0].sort_values(ascending=False)
    stats = {
        "last_date": str(last.date()),
        "n_held": int(len(held)),
        "codes": list(held.index),
    }
    lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
    for i, (code, weight) in enumerate(held.items(), 1):
        lines.append(
            f"| {i} | {code} | {names.get(code, '')} | {float(weight):.4f} |"
        )
    return "\n".join(lines), stats


def write_report(run_dir: Path, result: dict, names: dict[str, str], listed_n: int) -> None:
    metrics = result["metrics"]
    validation = result["validation"] or {}
    mc = validation.get("monte_carlo") or validation
    hold_md, hold = _holdings_table(run_dir, names)
    body = f"""# F0' 最优确认报告 — 创新药 BK1106（164 + 129）

**声明：研究/回测结果，非实盘建议。**

| 项 | 值 |
|----|----|
| 板块 | **创新药 BK1106** |
| 策略 | F0' = 动量 `gtja191_164` + 硬确认 `gtja191_129` |
| 成分来源 | 东财概念「创新药」BK1106 ∪ 原面板；排除北交所；原始 {listed_n} → 有效 OHLCV {result['n_symbols']} |
| 区间 | {START_DATE} → {END_DATE} |
| run | `{run_dir.name}` |

## 核心指标

| 指标 | 值 |
|------|----|
| total_return | {_pct(metrics.get('total_return'))} |
| excess_return | {_pct(metrics.get('excess_return'))} |
| sharpe | {_num(metrics.get('sharpe'))} |
| max_drawdown | {_pct(metrics.get('max_drawdown'))} |
| MC p_sharpe | {_num(mc.get('p_value_sharpe'))} |

## 最近持仓（{hold['last_date']}，n={hold['n_held']}）

{hold_md}

## 说明

1. 本版相对旧 97 只面板扩容：并入东财 BK1106 现有成分，并保留原面板中已不在板上的标的（含 **300142.SZ**）。
2. OHLCV 已按 {START_DATE}→{END_DATE} 重新拉取（修复旧缓存约从 2024-07 起、仅 ~500 根的截断）。
3. 确认腿仍为板块内存活因子重搜结果 `129`；扩池后指标与旧样本不可直接等同，须样本外复核。
4. 创新药政策/临床/估值波动大；研究/回测，非实盘建议。
"""
    (run_dir / "REPORT_F0_drug_129.md").write_text(body, encoding="utf-8")
    print(f"wrote {run_dir / 'REPORT_F0_drug_129.md'}")


def patch_prop_summary(result: dict, hold: dict, hold_md: str) -> None:
    """Refresh only the formal summary block at the bottom of the prop file."""
    if not PROP_OUT.exists():
        print(f"skip prop: missing {PROP_OUT}")
        return
    text = PROP_OUT.read_text(encoding="utf-8")
    marker = "## 正式复跑摘要"
    head = text.split(marker, 1)[0].rstrip()
    metrics = result["metrics"]
    validation = result["validation"] or {}
    mc = validation.get("monte_carlo") or validation
    summary = f"""## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **F0** / `129` |
| F0' run | `{F0_RUN.name}` |
| 成分 | 东财 BK1106 ∪ 原面板；有效 OHLCV **{result['n_symbols']}**（含 300142.SZ） |
| excess / sharpe / max_dd | {_pct(metrics.get('excess_return'))} / {_num(metrics.get('sharpe'))} / {_pct(metrics.get('max_drawdown'))} |
| MC p_sharpe | {_num(mc.get('p_value_sharpe'))} |
| 最近持仓日 | {hold['last_date']}（n={hold['n_held']}） |

{hold_md}

研究/回测，非实盘建议。
"""
    PROP_OUT.write_text(head + "\n\n" + summary, encoding="utf-8")
    print(f"refreshed {PROP_OUT}")


def main() -> None:
    if not F0_RUN.exists():
        raise SystemExit(f"missing formal run: {F0_RUN}")

    prior_cfg = json.loads((F0_RUN / "config.json").read_text(encoding="utf-8"))
    prior_codes = list(prior_cfg.get("codes") or [])
    print(f"[prior] codes={len(prior_codes)} has_300142={'300142.SZ' in prior_codes}")

    board = fetch_bk1106()
    univ = build_universe(prior_codes, board)
    listed_n = len(univ)
    print(
        f"[universe] union={listed_n} "
        f"from_board={(univ['source'] == 'BK1106').sum()} "
        f"prior_only={(univ['source'] == 'prior_panel').sum()} "
        f"has_300142={'300142.SZ' in set(univ['ts_code'])}"
    )

    UNIV_RUN.mkdir(parents=True, exist_ok=True)
    univ.to_csv(UNIV_RUN / "universe.csv", index=False, encoding="utf-8-sig")
    names = {str(r.ts_code): str(r.name) for r in univ.itertuples() if r.name}
    (UNIV_RUN / "universe_meta.json").write_text(
        json.dumps(
            {
                "source": f"东财概念「创新药」{BK} ∪ 原面板（强制保留 300142.SZ）",
                "bk": BK,
                "listed_n": listed_n,
                "start_date": START_DATE,
                "end_date": END_DATE,
                "names": names,
                "codes": univ["ts_code"].tolist(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    univ_art = UNIV_RUN / "artifacts"
    _seed_cache_from_prior(univ_art, F0_RUN / "artifacts")
    data_map = fetch_ohlcv(univ["ts_code"].tolist(), univ_art)
    if "300142.SZ" not in data_map:
        raise SystemExit("300142.SZ OHLCV missing after fetch — abort")
    if len(data_map) < 80:
        raise SystemExit(f"too few OHLCV after fetch: {len(data_map)}")

    codes = [c for c in univ["ts_code"].tolist() if c in data_map]
    print(f"[panel] effective={len(codes)} (dropped={listed_n - len(codes)})")

    result = run_backtest(F0_RUN, data_map, codes)
    write_report(F0_RUN, result, names, listed_n)
    hold_md, hold = _holdings_table(F0_RUN, names)
    patch_prop_summary(result, hold, hold_md)

    if COMPARE_OUT.exists():
        # Light touch: stamp new n_symbols into the compare header if present.
        cmp_text = COMPARE_OUT.read_text(encoding="utf-8")
        COMPARE_OUT.write_text(
            cmp_text.replace(
                "有效 97 只",
                f"有效 {result['n_symbols']} 只（BK1106∪原面板，含 300142.SZ）",
            ),
            encoding="utf-8",
        )
        print(f"patched {COMPARE_OUT}")

    m = result["metrics"]
    print(
        f"[done] n={result['n_symbols']} excess={m.get('excess_return')} "
        f"sharpe={m.get('sharpe')} last_hold={hold}"
    )


if __name__ == "__main__":
    main()
