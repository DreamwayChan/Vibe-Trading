"""Search GTJA191 alive confirm legs on CRO boards and publish frontend runs.

Boards:
  - bk1600: 医疗研发外包 BK1600 (default; already published)
  - bk0899: CRO 概念 BK0899

Same process family as 金属铜 BK1615 / 存储芯片 BK1137:
  1) Eastmoney constituents (drop BJ; market-cap cap 80)
  2) Download OHLCV (akshare → eastmoney → tushare)
  3) Confirm-leg search among alive gtja191 (mom=164, ban reverse 114)
  4) Formal F0' / F1' / legacy 171+054 artifacts + REPORT + req.json

Run from agent/:
    python scripts/research_cro_confirm_search.py
    python scripts/research_cro_confirm_search.py --board bk0899
    python scripts/research_cro_confirm_search.py --board bk0899 --skip-search
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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

from scripts.research_sector_confirm_search import (  # noqa: E402
    Sector,
    _load_alive_irs,
    _name_map,
    _search_sector,
)
from scripts.run_sectors_optimal_researched import (  # noqa: E402
    OptimalJob,
    _write_reports,
)

START_DATE = "2024-01-01"
END_DATE = "2026-08-20"
MIN_BARS = 120
MAX_UNIVERSE = 80
INITIAL_CASH = 1_000_000.0
MC_SIMS_DEFAULT = 1000
PAIR_POOL_DEFAULT = 15
# Amount-gated alives that the engine may use; 114 stays banned.
AMOUNT_GATED = frozenset({"gtja191_070", "gtja191_124", "gtja191_156"})
# Sibling universe whose OHLCV can seed a later board (BK1600 ⊂ CRO).
_OHLCV_SEED_UNIV = _AGENT_ROOT / "runs" / "20260821_160000_00_cro_universe"


@dataclass(frozen=True)
class BoardSpec:
    """One Eastmoney CRO-related board and the run/catalog ids it publishes."""

    key: str
    bk: str
    name: str
    slug: str
    run_prefix: str
    risk_note: str

    @property
    def panel(self) -> str:
        return f"{self.name} {self.bk}"

    @property
    def universe_desc(self) -> str:
        return f"东财概念「{self.name}」{self.bk}（排除北交所；市值优先最多{MAX_UNIVERSE}只）"

    @property
    def univ_run(self) -> Path:
        return _AGENT_ROOT / "runs" / f"{self.run_prefix}_00_{self.slug}_universe"

    @property
    def search_root(self) -> Path:
        return _AGENT_ROOT / "runs" / f"_research_{self.slug}_confirm_search"

    @property
    def prop_out(self) -> Path:
        return _AGENT_ROOT / f"prop_国泰君安_{self.name}_最优.md"

    @property
    def universe_json(self) -> Path:
        return _AGENT_ROOT / "runs" / f"_tmp_{self.slug}_universe" / f"{self.bk.lower()}.json"

    @property
    def winner_json(self) -> Path:
        return self.search_root / "winner.json"


BOARDS: dict[str, BoardSpec] = {
    "bk1600": BoardSpec(
        key="cro",
        bk="BK1600",
        name="医疗研发外包",
        slug="cro",
        run_prefix="20260821_160000",
        risk_note=(
            "CXO 海外订单/BD 波动、地缘与生物安全、估值与集采外溢；"
            "确认腿样本内重搜有过拟合风险"
        ),
    ),
    "bk0899": BoardSpec(
        key="cro0899",
        bk="BK0899",
        name="CRO",
        slug="cro0899",
        run_prefix="20260821_163600",
        risk_note=(
            "CRO 海外订单/BD 波动、地缘与生物安全、估值与集采外溢；"
            "确认腿样本内重搜有过拟合风险"
        ),
    ),
}

# Active board — ``_activate`` fills these so the rest of the file stays linear.
SPEC = BOARDS["bk1600"]
BK_CODE = SPEC.bk
BK_NAME = SPEC.name
PANEL_LABEL = SPEC.panel
UNIVERSE_DESC = SPEC.universe_desc
RUN_PREFIX = SPEC.run_prefix
SLUG = SPEC.slug
JOB_KEY = SPEC.key
UNIV_RUN = SPEC.univ_run
SEARCH_ROOT = SPEC.search_root
PROP_OUT = SPEC.prop_out
UNIVERSE_JSON = SPEC.universe_json
WINNER_JSON = SPEC.winner_json
RISK_NOTE = SPEC.risk_note


def _activate(spec: BoardSpec) -> None:
    """Bind module-level paths/labels to ``spec`` before universe/search/publish."""
    global SPEC, BK_CODE, BK_NAME, PANEL_LABEL, UNIVERSE_DESC, RUN_PREFIX, SLUG
    global JOB_KEY, UNIV_RUN, SEARCH_ROOT, PROP_OUT, UNIVERSE_JSON, WINNER_JSON, RISK_NOTE
    SPEC = spec
    BK_CODE = spec.bk
    BK_NAME = spec.name
    PANEL_LABEL = spec.panel
    UNIVERSE_DESC = spec.universe_desc
    RUN_PREFIX = spec.run_prefix
    SLUG = spec.slug
    JOB_KEY = spec.key
    UNIV_RUN = spec.univ_run
    SEARCH_ROOT = spec.search_root
    PROP_OUT = spec.prop_out
    UNIVERSE_JSON = spec.universe_json
    WINNER_JSON = spec.winner_json
    RISK_NOTE = spec.risk_note


def _sector() -> Sector:
    """Sector shim consumed by the shared confirm-search engine."""
    return Sector(
        key=JOB_KEY,
        name=BK_NAME,
        panel=PANEL_LABEL,
        universe_desc=UNIVERSE_DESC,
        ohlcv_src=UNIV_RUN,
        start_date=START_DATE,
        end_date=END_DATE,
        initial_cash=INITIAL_CASH,
        risk_note=RISK_NOTE,
        out_root=SEARCH_ROOT,
        prop_out=PROP_OUT,
        default_pair_pool=PAIR_POOL_DEFAULT,
    )


def _is_bj(code: str) -> bool:
    digits = code.split(".")[0]
    return digits.startswith(("4", "8", "92")) or code.upper().endswith(".BJ")


def fetch_universe() -> dict:
    """Pull the active board's constituents, largest market-cap first (fid=f20)."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        }
    )
    all_rows: list[dict] = []
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
            fs=f"b:{BK_CODE}",
            fields="f12,f13,f14,f20",
        )
        response = session.get(
            "https://push2delay.eastmoney.com/api/qt/clist/get",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        total = data.get("total", total)
        diff = data.get("diff") or []
        if not diff:
            break
        for item in diff:
            code = str(item.get("f12") or "").zfill(6)
            market = int(item.get("f13") or 0)
            if market == 1:
                ts_code = f"{code}.SH"
            elif code.startswith(("4", "8", "92")):
                ts_code = f"{code}.BJ"
            else:
                ts_code = f"{code}.SZ"
            all_rows.append(
                {
                    "ts_code": ts_code,
                    "name": str(item.get("f14") or ""),
                    "market": market,
                    "mv": item.get("f20"),
                }
            )
        if total and len(all_rows) >= int(total):
            break
        time.sleep(0.08)

    payload = {
        "n": len(all_rows),
        "total": total,
        "bk": BK_CODE,
        "name": BK_NAME,
        "codes": all_rows,
    }
    UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[universe] {BK_CODE} {BK_NAME} n={len(all_rows)} total={total}")
    return payload


def _normalize_universe(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for item in raw:
        code = str(item.get("ts_code") or "").upper()
        name = str(item.get("name") or "")
        if not code or "." not in code:
            continue
        if _is_bj(code) or code.split(".")[0].startswith("92"):
            continue
        rows.append({"ts_code": code, "name": name, "board": PANEL_LABEL})
    return (
        pd.DataFrame(rows)
        .drop_duplicates("ts_code")
        .reset_index(drop=True)  # keep Eastmoney market-cap order
    )


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
    """Prefer akshare/eastmoney: tushare is flaky; tencent caps at 500 bars."""
    errors: list[str] = []
    for fetcher in (_fetch_one_akshare, _fetch_one_eastmoney, _fetch_one_tushare):
        code, frame, err = fetcher(code)
        if frame is not None and _coverage_ok(frame):
            return code, frame, ""
        if err:
            errors.append(f"{fetcher.__name__}:{err}")
    return code, None, " | ".join(errors) or "short/late"


def fetch_ohlcv(codes: list[str], art: Path) -> dict[str, pd.DataFrame]:
    """Download (or reuse) per-symbol OHLCV into ``art``."""
    art.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    pending: list[str] = []

    seed_art = _OHLCV_SEED_UNIV / "artifacts"
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        # Reuse BK1600 bars when this board overlaps (CRO ⊃ 医疗研发外包).
        if not path.exists() and seed_art.is_dir() and seed_art.resolve() != art.resolve():
            seeded = seed_art / f"ohlcv_{code}.csv"
            if seeded.exists():
                shutil.copy2(seeded, path)
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
            if ok % 10 == 0:
                print(f"  progress ok={ok} fail={fail}")
    print(
        f"[fetch] done total={len(data_map)} new_ok={ok} fail={fail} "
        f"{time.time() - t0:.1f}s"
    )
    return data_map


def build_universe() -> pd.DataFrame:
    """Write universe.csv + OHLCV cache under the dated universe run."""
    payload = fetch_universe()
    univ = _normalize_universe(payload["codes"])
    listed_n = len(payload["codes"])
    if len(univ) > MAX_UNIVERSE:
        univ = univ.head(MAX_UNIVERSE).copy()
    used_n = len(univ)
    print(f"[universe] raw={listed_n} after_ex_BJ_cap={used_n}")
    if used_n < 8:
        raise SystemExit(f"{BK_NAME}: universe too small: {used_n}")

    UNIV_RUN.mkdir(parents=True, exist_ok=True)
    univ.to_csv(UNIV_RUN / "universe.csv", index=False, encoding="utf-8-sig")
    (UNIV_RUN / "universe_meta.json").write_text(
        json.dumps(
            {
                "panel": PANEL_LABEL,
                "source": f"eastmoney concept {BK_CODE}",
                "listed_raw": listed_n,
                "ex_bj_capped": used_n,
                "max_universe": MAX_UNIVERSE,
                "codes": univ["ts_code"].tolist(),
                "names": {r.ts_code: r.name for r in univ.itertuples()},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    data_map = fetch_ohlcv(univ["ts_code"].tolist(), UNIV_RUN / "artifacts")
    if len(data_map) < 8:
        raise SystemExit(f"{BK_NAME}: too few OHLCV: {len(data_map)}")
    codes = [c for c in univ["ts_code"].tolist() if c in data_map]
    (UNIV_RUN / "config.json").write_text(
        json.dumps(
            {
                "codes": codes,
                "source": "tushare",
                "start_date": START_DATE,
                "end_date": END_DATE,
                "interval": "1D",
                "initial_cash": INITIAL_CASH,
                "parameters": {"panel": PANEL_LABEL, "universe": UNIVERSE_DESC},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[universe] effective OHLCV={len(data_map)}")
    return univ


def _load_metrics_bundle(run_dir: Path) -> dict:
    """Pack a completed engine run into the shape ``_write_reports`` expects."""
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    params = cfg.get("parameters") or {}
    confirm_ids = list(params.get("confirm_ids") or [])
    label = "+".join(a.replace("gtja191_", "") for a in confirm_ids) or "pure_164"
    return {
        "metrics": metrics,
        "validation": validation,
        "confirm_ids": confirm_ids,
        "label": label,
        "run_dir": str(run_dir),
        "n_symbols": len(cfg.get("codes") or []),
        "is_rebalance_days": metrics.get("is_rebalance_days") or metrics.get("rebalance_count"),
        "veto_max": bool(params.get("veto_max")),
    }


def _discover_search_results(out_root: Path) -> tuple[dict, dict | None, dict]:
    """Pick MC-verified F0 / F1 / legacy 171+054 from the search tree."""
    full_dirs = [
        path
        for path in out_root.iterdir()
        if path.is_dir()
        and path.name.startswith("full_")
        and not path.name.endswith("_F1")
        and (path / "artifacts" / "metrics.csv").exists()
    ]
    if not full_dirs:
        raise SystemExit(f"no MC full_* runs under {out_root}")

    verified = [_load_metrics_bundle(path) for path in full_dirs]
    best_f0 = max(verified, key=lambda row: float(row["metrics"]["excess_return"]))
    legacy = next((row for row in verified if row["label"] == "171+054"), None)
    if legacy is None:
        raise SystemExit("missing MC baseline full_171+054")

    f1_dir = out_root / f"full_{best_f0['label']}_F1"
    f1_result = None
    if (f1_dir / "artifacts" / "metrics.csv").exists():
        f1_result = _load_metrics_bundle(f1_dir)
        f1_result["confirm_ids"] = list(best_f0["confirm_ids"])
        f1_result["label"] = best_f0["label"]
    return best_f0, f1_result, legacy


def _amount_note(confirm_ids: list[str]) -> str:
    gated = [aid for aid in confirm_ids if aid in AMOUNT_GATED]
    if not gated:
        return "不要为 114 拼 amount"
    pretty = "/".join(aid.replace("gtja191_", "") for aid in gated)
    return f"gtja191_{pretty} 需要 amount，允许 close×volume 合成；禁止为 114 拼 amount"


def _copy_run(src: Path, dst: Path, data_map: dict[str, pd.DataFrame]) -> None:
    """Publish a search run into a dated frontend directory, with OHLCV copies.

    Charts on the run-detail page read ``artifacts/ohlcv_*.csv`` first; the
    search engine never wrote those files because it used an in-memory loader.
    """
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    art = dst / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    for code, frame in data_map.items():
        out = frame.copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")


def _stamp_ohlcv_from_universe() -> dict[str, pd.DataFrame]:
    art = UNIV_RUN / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for path in sorted(art.glob("ohlcv_*.csv")):
        code = path.stem.replace("ohlcv_", "", 1)
        frame = _normalize_ohlcv_frame(pd.read_csv(path))
        if _coverage_ok(frame):
            data_map[code] = frame
    if len(data_map) < 8:
        raise SystemExit(f"universe OHLCV missing under {UNIV_RUN}")
    return data_map


def publish_formal(best_f0: dict, f1_result: dict | None, legacy: dict) -> str:
    """Copy MC-verified runs into dated dirs and write frontend REPORT/req.json."""
    if f1_result is None:
        raise SystemExit("F1 MAX-veto run missing — cannot publish a complete pair")

    confirm_ids = tuple(best_f0["confirm_ids"])
    conf = best_f0["label"]
    f0_run = _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_03_F0_{SLUG}_{conf.replace('+', '_')}"
    f1_run = _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_04_F1_{SLUG}_{conf.replace('+', '_')}"
    legacy_run = _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_03_F0_{SLUG}_legacy171054"

    data_map = _stamp_ohlcv_from_universe()
    _copy_run(Path(best_f0["run_dir"]), f0_run, data_map)
    _copy_run(Path(f1_result["run_dir"]), f1_run, data_map)
    _copy_run(Path(legacy["run_dir"]), legacy_run, data_map)

    sec = _sector()
    job = OptimalJob(
        key=JOB_KEY,
        name=BK_NAME,
        panel=PANEL_LABEL,
        universe_desc=UNIVERSE_DESC,
        ohlcv_src=UNIV_RUN,
        start_date=START_DATE,
        end_date=END_DATE,
        initial_cash=INITIAL_CASH,
        risk_note=sec.risk_note,
        confirm_ids=confirm_ids,
        amount_note=_amount_note(list(confirm_ids)),
        f0_run=f0_run,
        f1_run=f1_run,
        legacy_f0_run=legacy_run,
        prop_out=PROP_OUT,
        published_f1=None,
        out_root=SEARCH_ROOT,
    )
    names = _name_map(sec)
    irs = _load_alive_irs()
    for aid in list(confirm_ids) + ["gtja191_164", "gtja191_171", "gtja191_054"]:
        irs.setdefault(aid, 0.10)

    f0 = _load_metrics_bundle(f0_run)
    f1 = _load_metrics_bundle(f1_run)
    f1["confirm_ids"] = list(confirm_ids)
    f1["label"] = conf
    leg = _load_metrics_bundle(legacy_run)
    winner = _write_reports(job, f0, f1, leg, irs, names)

    winner_payload = {
        "winner": winner,
        "confirm_ids": list(confirm_ids),
        "label": conf,
        "f0_run": f0_run.name,
        "f1_run": f1_run.name,
        "legacy_run": legacy_run.name,
        "use_max_veto": winner == "F1",
        "catalog_run_id": f1_run.name if winner == "F1" else f0_run.name,
    }
    SEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    WINNER_JSON.write_text(
        json.dumps(winner_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[publish] winner={winner} confirm={conf}")
    print(f"  F0 {f0_run.name}")
    print(f"  F1 {f1_run.name}")
    print(f"  wrote {WINNER_JSON}")
    return winner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--board",
        choices=tuple(BOARDS),
        default="bk1600",
        help="Eastmoney board to search (default: 医疗研发外包 BK1600)",
    )
    parser.add_argument("--skip-universe", action="store_true")
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--pair-pool", type=int, default=PAIR_POOL_DEFAULT)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--mc-sims", type=int, default=MC_SIMS_DEFAULT)
    parser.add_argument("--no-pure", action="store_true")
    args = parser.parse_args()
    _activate(BOARDS[args.board])
    print(f"[board] {BK_CODE} {BK_NAME} slug={SLUG} prefix={RUN_PREFIX}")

    if not args.skip_universe:
        build_universe()
    elif not (UNIV_RUN / "artifacts").exists():
        raise SystemExit(f"--skip-universe but missing {UNIV_RUN}")

    sec = _sector()
    if not args.skip_search:
        irs = _load_alive_irs()
        if "gtja191_164" not in irs:
            raise SystemExit("gtja191_164 not in alive set")
        print(f"[survival] alive gtja191={len(irs)}")
        _search_sector(
            sec,
            irs,
            pair_pool=args.pair_pool,
            top_k=args.top_k,
            mc_sims=args.mc_sims,
            include_pure=(not args.no_pure),
        )
    elif not (SEARCH_ROOT / "confirm_search_rank.csv").exists():
        raise SystemExit(f"--skip-search but missing rank csv under {SEARCH_ROOT}")

    best_f0, f1_result, legacy = _discover_search_results(SEARCH_ROOT)
    print(
        f"[discover] best F0={best_f0['label']} "
        f"excess={best_f0['metrics'].get('excess_return')} "
        f"F1={'yes' if f1_result else 'no'}"
    )
    publish_formal(best_f0, f1_result, legacy)
    print("[done]")


if __name__ == "__main__":
    main()
