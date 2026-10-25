"""Search confirm-leg combos among GTJA191 alive factors.

Sectors: robot / memory / foodbev / compute / drug / speceq / aiapp / machine / vrar / autoparts.


Fixed mom = gtja191_164. Confirm legs freely combined from alive factors
(singles + pairs), excluding mom and banned reverse 114.

Pipeline (full engine, no fast-path ranking bias):
  1) Score all singles + baseline 171/054 + pure_164
  2) Form pairs among top `--pair-pool` singles by excess
  3) Re-verify Top-K with MC; also test F1 (MAX veto) on the best F0 confirm
  4) Write summary + update prop_国泰君安_{name}_最优.md

Run from agent/:
    python scripts/research_sector_confirm_search.py
    python scripts/research_sector_confirm_search.py --only foodbev
    python scripts/research_sector_confirm_search.py --only memory --pair-pool 15
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine  # noqa: E402
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402

# Reuse F0 portfolio construction + confirm mask from the copper search module.
from scripts.research_copper_confirm_search import (  # noqa: E402
    BANNED_CONFIRM,
    BASELINE_CONFIRM,
    MOM_ID,
    _build_weights,
    _confirm_ok,
    _smooth_cs,
)

ENGINE_F0_SRC = (
    _AGENT_ROOT / "runs" / "_research_custom_positive" / "F0" / "code" / "signal_engine.py"
)
ENGINE_F1_SRC = (
    _AGENT_ROOT
    / "runs"
    / "_research_custom_positive"
    / "veto_max_20"
    / "code"
    / "signal_engine.py"
)

FALLBACK_IR = {
    "gtja191_164": 0.145,
    "gtja191_171": 0.155,
    "gtja191_054": 0.152,
    "gtja191_081": 0.121,
    "gtja191_160": 0.145,
    "gtja191_124": 0.10,
    "gtja191_178": 0.10,
    "gtja191_185": 0.10,
    "gtja191_070": 0.10,
}

# Template _build_panel only exposes OHLCV. Amount-gated alives (070/124) need
# synthetic amount=close*volume so Monitor / regenerate can load the same engine.
_PANEL_WITHOUT_AMOUNT = """    def _build_panel(self, data_map):
        cf, of_, hf, lf, vf = {}, {}, {}, {}, {}
        for code, df in data_map.items():
            if df is None or df.empty:
                continue
            cf[code] = df["close"]; of_[code] = df["open"]
            hf[code] = df["high"]; lf[code] = df["low"]; vf[code] = df["volume"]
        if not cf:
            return None
        return {
            "close": pd.DataFrame(cf), "open": pd.DataFrame(of_),
            "high": pd.DataFrame(hf), "low": pd.DataFrame(lf),
            "volume": pd.DataFrame(vf),
        }"""

_PANEL_WITH_AMOUNT = """    def _build_panel(self, data_map):
        # Synthetic amount for amount-gated alives (070/124). Reverse-114 stays banned.
        cf, of_, hf, lf, vf = {}, {}, {}, {}, {}
        for code, df in data_map.items():
            if df is None or df.empty:
                continue
            cf[code] = df["close"]; of_[code] = df["open"]
            hf[code] = df["high"]; lf[code] = df["low"]; vf[code] = df["volume"]
        if not cf:
            return None
        close = pd.DataFrame(cf)
        volume = pd.DataFrame(vf)
        return {
            "close": close, "open": pd.DataFrame(of_),
            "high": pd.DataFrame(hf), "low": pd.DataFrame(lf),
            "volume": volume,
            "amount": close * volume,
        }"""


def _write_signal_engine(
    dst: Path,
    confirm_ids: list[str],
    irs: dict[str, float],
    *,
    veto_max: bool = False,
) -> None:
    """Persist a loadable SignalEngine for Monitor / strategy_ready listing.

    Research ``_full_run`` used an in-process cached engine and historically
    left ``code/`` empty — Monitor filters those runs out via strategy_ready.
    """
    import re

    src = ENGINE_F1_SRC if veto_max else ENGINE_F0_SRC
    if not src.is_file():
        raise SystemExit(f"engine template missing: {src}")

    text = src.read_text(encoding="utf-8")

    ir_lines = [f'        "{MOM_ID}": {float(irs.get(MOM_ID, FALLBACK_IR[MOM_ID])):.4f},']
    for aid in confirm_ids:
        ir_lines.append(
            f'        "{aid}": {float(irs.get(aid, FALLBACK_IR.get(aid, 0.10))):.4f},'
        )
    ir_block = "    _IR = {\n" + "\n".join(ir_lines) + "\n    }"
    start = text.find("    _IR = {")
    if start < 0:
        raise SystemExit("engine missing _IR block")
    end = text.find("}", start)
    end = text.find("\n", end)
    text = text[:start] + ir_block + text[end:]

    new_conf = f"self.confirm_ids = {json.dumps(list(confirm_ids))}"
    text2, n = re.subn(r"self\.confirm_ids = \[[^\]]*\]", new_conf, text, count=1)
    if n != 1:
        raise SystemExit("engine missing confirm_ids assignment")
    text = text2

    if _PANEL_WITHOUT_AMOUNT not in text:
        raise SystemExit("engine _build_panel template drift — cannot inject amount")
    text = text.replace(_PANEL_WITHOUT_AMOUNT, _PANEL_WITH_AMOUNT, 1)

    if veto_max and "custom_max_inv" not in text:
        raise SystemExit("F1 engine template missing custom_max_inv")

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class Sector:
    key: str
    name: str
    panel: str
    universe_desc: str
    ohlcv_src: Path
    start_date: str
    end_date: str
    initial_cash: float
    risk_note: str
    out_root: Path
    prop_out: Path
    # Default pair-pool size (0 = all pairs among candidates — heavy).
    default_pair_pool: int


SECTORS = [
    Sector(
        key="robot",
        name="人形机器人",
        panel="人形机器人 BK1184",
        universe_desc="东财概念 人形机器人 BK1184（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260725_184000_01_F1_robot",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=10_000_000,
        risk_note="主题拥挤、订单兑现与估值波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_robot_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_人形机器人_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="memory",
        name="存储芯片",
        panel="存储芯片 BK1137",
        universe_desc="东财 存储芯片 BK1137（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260731_tech3_00_memory_universe",
        start_date="2024-01-01",
        end_date="2026-07-31",
        initial_cash=1_000_000,
        risk_note="存储周期、HBM/DRAM 涨价节奏与海外巨头扩产扰动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_memory_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_存储芯片_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="foodbev",
        name="食品饮料",
        panel="食品饮料 BK0438",
        universe_desc="东财行业「食品饮料」BK0438（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_foodbev_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="白酒估值分化、消费景气与成本周期；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_foodbev_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_食品饮料_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="compute",
        name="算力硬件",
        panel="算力硬件（光模块∪PCB∪液冷）",
        universe_desc="东财 光通信模块 BK1136 ∪ 印制电路板 BK1340 ∪ 液冷概念 BK1138（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260731_tech3_00_compute_universe",
        start_date="2024-01-01",
        end_date="2026-07-31",
        initial_cash=1_000_000,
        risk_note="算力链景气、光模块/PCB/液冷主题轮动与估值波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_compute_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_算力硬件_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="drug",
        name="创新药",
        panel="创新药 BK1106",
        universe_desc="东财概念「创新药」BK1106 ∪ 原面板（排除北交所；含 300142.SZ）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260807_drug_00_universe",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=10_000_000,
        risk_note="创新药政策/临床/估值波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_drug_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_创新药_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="speceq",
        name="其他专用设备",
        panel="其他专用设备 BK1405",
        universe_desc="东财行业「其他专用设备」BK1405（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_speceq_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="设备订单/资本开支周期与板块轮动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_speceq_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_其他专用设备_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="aiapp",
        name="AI应用",
        panel="AI应用",
        universe_desc="A股 AI 应用相关标的池（沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260725_193311_93_84c623",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="AI 应用主题轮动、业绩兑现差与高波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_aiapp_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_AI应用_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="machine",
        name="工业母机",
        panel="工业母机 BK1004",
        universe_desc="东财概念 工业母机 BK1004（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260726_084500_01_F0_machine",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="工业母机订单/设备周期与国产替代叙事波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_machine_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_工业母机_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="vrar",
        name="虚拟现实",
        panel="虚拟现实 BK0722",
        universe_desc="东财概念「虚拟现实」BK0722（排除北交所；沿用既有面板 OHLCV）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260726_123500_00_VRAR_universe",
        start_date="2024-01-01",
        end_date="2026-07-24",
        initial_cash=1_000_000,
        risk_note="VR/AR 产品周期、消费电子景气与主题炒作波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_vrar_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_虚拟现实_最优.md",
        default_pair_pool=15,
    ),
    Sector(
        key="autoparts",
        name="其他汽车零部件",
        panel="其他汽车零部件 BK1528",
        universe_desc="东财行业「其他汽车零部件」BK1528（排除北交所；市值优先最多80只）",
        ohlcv_src=_AGENT_ROOT / "runs" / "20260730_162800_00_autoparts_universe",
        start_date="2024-01-01",
        end_date="2026-07-28",
        initial_cash=1_000_000,
        risk_note="汽车产业链景气、新能源渗透与零部件价格波动；确认腿样本内重搜有过拟合风险",
        out_root=_AGENT_ROOT / "runs" / "_research_autoparts_confirm_search",
        prop_out=_AGENT_ROOT / "prop_国泰君安_其他汽车零部件_最优.md",
        default_pair_pool=15,
    ),
]


def _load_alive_irs() -> dict[str, float]:
    path = survival_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or payload
    out: dict[str, float] = {}
    for aid, row in alphas.items():
        if not str(aid).startswith("gtja191_"):
            continue
        if not isinstance(row, dict) or row.get("category") != "alive":
            continue
        try:
            out[str(aid)] = float(row.get("ir") or FALLBACK_IR.get(aid, 0.10))
        except (TypeError, ValueError):
            out[str(aid)] = float(FALLBACK_IR.get(aid, 0.10))
    return out


def _load_ohlcv(src: Path) -> dict[str, pd.DataFrame]:
    art = src / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for path in sorted(art.glob("ohlcv_*.csv")):
        code = path.stem.replace("ohlcv_", "", 1)
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if len(df) >= 120 and "close" in keep:
            data_map[code] = df[keep].copy()
    if len(data_map) < 8:
        raise SystemExit(f"{src.name}: too few OHLCV ({len(data_map)})")
    return data_map


def _build_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """OHLCV + synthetic amount for amount-gated alives (114 still banned)."""
    cols = sorted(data_map)
    close = pd.DataFrame({c: data_map[c]["close"] for c in cols})
    volume = pd.DataFrame({c: data_map[c]["volume"] for c in cols})
    return {
        "close": close,
        "open": pd.DataFrame({c: data_map[c]["open"] for c in cols}),
        "high": pd.DataFrame({c: data_map[c]["high"] for c in cols}),
        "low": pd.DataFrame({c: data_map[c]["low"] for c in cols}),
        "volume": volume,
        "amount": close * volume,
    }


def _precompute_scores(
    panel: dict[str, pd.DataFrame], alive_ids: list[str]
) -> dict[str, pd.DataFrame]:
    reg = Registry()
    scores: dict[str, pd.DataFrame] = {}
    for aid in alive_ids:
        t0 = time.time()
        try:
            raw = reg.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {aid}: {exc}")
            continue
        if raw is None or raw.empty:
            print(f"  [skip] {aid}: empty")
            continue
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = _smooth_cs(raw)
        nn = int(scores[aid].notna().to_numpy().sum())
        print(f"  factor {aid}: non_null={nn} ({time.time() - t0:.2f}s)")
    return scores


def _combo_label(confirm_ids: tuple[str, ...] | list[str]) -> str:
    if not confirm_ids:
        return "pure_164"
    return "+".join(aid.replace("gtja191_", "") for aid in confirm_ids)


class _CachedSignalEngine:
    """F0 engine using precomputed smooth CS scores; optional MAX veto."""

    def __init__(
        self,
        confirm_ids: list[str],
        scores: dict[str, pd.DataFrame],
        irs: dict[str, float],
        *,
        veto_max: bool = False,
    ):
        self.mom_id = MOM_ID
        self.confirm_ids = list(confirm_ids)
        self.alpha_ids = [self.mom_id] + self.confirm_ids
        if veto_max:
            self.alpha_ids.append("custom_max_inv")
        self.scores = scores
        self.irs = irs
        self.veto_max = veto_max
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).sort_index()
        mom = self.scores[self.mom_id].reindex(index=close.index, columns=close.columns)
        ok = _confirm_ok(tuple(self.confirm_ids), self.scores, self.irs, close)

        # F1: block NEW entries only when custom_max_inv in bottom 20% CS.
        # Existing holdings keep via keep_candidates path (confirm not re-checked).
        if self.veto_max and "custom_max_inv" in self.scores:
            mx = self.scores["custom_max_inv"].reindex(
                index=close.index, columns=close.columns
            )
            thr = mx.quantile(0.20, axis=1)
            veto_ok = mx.ge(thr, axis=0).fillna(True)
            ok = ok & veto_ok

        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _full_run(
    sec: Sector,
    confirm_ids: list[str],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame],
    run_dir: Path,
    *,
    mc_sims: int,
    veto_max: bool = False,
) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    # Ensure custom_max_inv score exists when vetoing.
    if veto_max and "custom_max_inv" not in scores:
        reg = Registry()
        panel = _build_panel(data_map)
        raw = reg.compute("custom_max_inv", panel)
        if raw is None or raw.empty:
            raise SystemExit("custom_max_inv empty")
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores["custom_max_inv"] = _smooth_cs(raw)

    alphas = [MOM_ID] + list(confirm_ids)
    if veto_max:
        alphas.append("custom_max_inv")
    tag = _combo_label(confirm_ids) + ("_F1" if veto_max else "_F0")
    cfg = {
        "codes": sorted(data_map),
        "source": "tushare",
        "start_date": sec.start_date,
        "end_date": sec.end_date,
        "interval": "1D",
        "initial_cash": sec.initial_cash,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": alphas,
        "parameters": {
            "strategy": f"confirm_search_{tag}",
            "panel": sec.panel,
            "confirm_ids": list(confirm_ids),
            "veto_max": veto_max,
        },
        "validation": {"monte_carlo": {"n_simulations": mc_sims}},
        "_run_card_effective_sources": ["local_artifacts"],
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Monitor UI lists runs with strategy_ready=true only if this file exists.
    _write_signal_engine(
        run_dir / "code" / "signal_engine.py",
        confirm_ids,
        irs,
        veto_max=veto_max,
    )

    sig = _CachedSignalEngine(confirm_ids, scores, irs, veto_max=veto_max)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "metrics": metrics,
        "validation": validation,
        "is_rebalance_days": sig.last_is_rebalance_days,
        "confirm_ids": list(confirm_ids),
        "label": _combo_label(confirm_ids),
        "veto_max": veto_max,
        "run_dir": str(run_dir),
    }


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


def _holdings_md(run_dir: Path, name_map: dict[str, str]) -> tuple[str, dict]:
    pos = pd.read_csv(Path(run_dir) / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    held = pos.loc[last]
    held = held[held > 0].sort_values(ascending=False)
    stats = {
        "last_date": str(last.date()),
        "n_held": int(len(held)),
        "gross": float(held.sum()),
        "codes": list(held.index),
    }
    lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
    for i, (code, w) in enumerate(held.items(), 1):
        lines.append(f"| {i} | {code} | {name_map.get(code, '')} | {float(w):.4f} |")
    return "\n".join(lines), stats


def _name_map(sec: Sector) -> dict[str, str]:
    """Load display names from the sector's universe artifacts when present."""
    root = sec.ohlcv_src
    meta = root / "universe_meta.json"
    if meta.exists():
        return {
            str(k): str(v)
            for k, v in (json.loads(meta.read_text(encoding="utf-8")).get("names") or {}).items()
        }
    csv_path = root / "universe.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        if "ts_code" in df.columns and "name" in df.columns:
            return {str(r.ts_code): str(r.name) for r in df.itertuples()}
    # Fallback: F1/F0 run dirs sometimes keep names under the same prefix.
    return {}


def _score_combo_cached(
    sec: Sector,
    confirm_ids: list[str],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame],
    cache: dict[str, dict],
    *,
    mc_sims: int = 0,
) -> dict:
    lab = _combo_label(confirm_ids)
    if lab in cache:
        return cache[lab]
    run_dir = sec.out_root / f"screen_{lab}"
    # Reuse on-disk metrics if present (resume-friendly).
    metrics_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_path.exists() and mc_sims == 0:
        m = pd.read_csv(metrics_path).iloc[0].to_dict()
        row = {
            "label": lab,
            "confirm_ids": list(confirm_ids),
            "n_confirm": len(confirm_ids),
            "is_baseline": list(confirm_ids) == list(BASELINE_CONFIRM),
            "total_return": float(m["total_return"]),
            "excess_return": float(m["excess_return"]),
            "sharpe": float(m["sharpe"]),
            "max_drawdown": float(m["max_drawdown"]),
            "benchmark_return": float(m.get("benchmark_return", 0.0)),
            "run_dir": str(run_dir),
            "metrics": m,
            "validation": {},
            "veto_max": False,
        }
        cache[lab] = row
        print(f"  [cache] {lab} excess={row['excess_return']:.4f}")
        return row

    print(f"  [run] {lab}")
    result = _full_run(
        sec, confirm_ids, scores, irs, data_map, run_dir, mc_sims=mc_sims, veto_max=False
    )
    m = result["metrics"]
    row = {
        "label": lab,
        "confirm_ids": list(confirm_ids),
        "n_confirm": len(confirm_ids),
        "is_baseline": list(confirm_ids) == list(BASELINE_CONFIRM),
        "total_return": float(m["total_return"]),
        "excess_return": float(m["excess_return"]),
        "sharpe": float(m["sharpe"]),
        "max_drawdown": float(m["max_drawdown"]),
        "benchmark_return": float(m.get("benchmark_return", 0.0)),
        "run_dir": str(run_dir),
        "metrics": m,
        "validation": result.get("validation") or {},
        "veto_max": False,
        "is_rebalance_days": result.get("is_rebalance_days"),
    }
    print(f"         excess={row['excess_return']:.4f} sharpe={row['sharpe']:.4f}")
    cache[lab] = row
    return row


def _write_outputs(
    sec: Sector,
    df: pd.DataFrame,
    verified: list[dict],
    f1_result: dict | None,
    name_map: dict[str, str],
    irs: dict[str, float],
) -> None:
    sec.out_root.mkdir(parents=True, exist_ok=True)
    csv_path = sec.out_root / "confirm_search_rank.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    base = df.loc[df["is_baseline"]]
    base_ex = float(base.iloc[0]["excess_return"]) if len(base) else 0.0

    # Pick best F0 among verified (highest excess).
    best_f0 = max(verified, key=lambda x: float(x["metrics"]["excess_return"]))
    # Final winner: F1 if it beats best F0 on excess, else F0.
    if f1_result is not None and float(f1_result["metrics"]["excess_return"]) > float(
        best_f0["metrics"]["excess_return"]
    ):
        winner = f1_result
        winner_kind = "F1"
    else:
        winner = best_f0
        winner_kind = "F0"

    lines = [
        f"# {sec.panel} — GTJA191 存活因子确认腿自由组合搜索",
        "",
        "**声明：研究/回测，非实盘建议。样本内重搜有过拟合风险。**",
        "",
        "## 设定",
        "",
        f"- 动量主排序固定：`{MOM_ID}`",
        f"- 候选：存活 gtja191 排除 `{MOM_ID}` 与禁止反转 `{sorted(BANNED_CONFIRM)}`",
        f"- 过程约束：周频 / C≥40% / N=6 / 换手帽20% / MA20 择时（与 F0 族一致）",
        f"- 完整引擎筛选组合数：{len(df)}",
        "",
        "## 完整引擎排行 Top20（按 excess）",
        "",
        "| rank | confirm | n | excess | total | sharpe | max_dd | baseline? |",
        "|-----:|---------|--:|-------:|------:|-------:|-------:|:---------:|",
    ]
    for i, row in enumerate(df.head(20).itertuples(index=False), 1):
        lines.append(
            f"| {i} | `{row.label}` | {row.n_confirm} | {_pct(row.excess_return)} | "
            f"{_pct(row.total_return)} | {_num(row.sharpe)} | {_pct(row.max_drawdown)} | "
            f"{'Y' if row.is_baseline else ''} |"
        )

    lines += [
        "",
        "## MC 复核 + F1 对照",
        "",
        "| label | mode | excess | total | sharpe | max_dd | MC p | Δex vs 171+054 |",
        "|-------|------|-------:|------:|-------:|-------:|-----:|---------------:|",
    ]
    for v in sorted(verified, key=lambda x: float(x["metrics"]["excess_return"]), reverse=True):
        m = v["metrics"]
        mc = (v.get("validation") or {}).get("monte_carlo") or {}
        lines.append(
            f"| `{v['label']}` | F0 | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_num(mc.get('p_value_sharpe'))} | "
            f"{_pct(float(m['excess_return']) - base_ex)} |"
        )
    if f1_result is not None:
        m = f1_result["metrics"]
        mc = (f1_result.get("validation") or {}).get("monte_carlo") or {}
        lines.append(
            f"| `{f1_result['label']}` | F1+MAX | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_num(mc.get('p_value_sharpe'))} | "
            f"{_pct(float(m['excess_return']) - base_ex)} |"
        )

    wm = winner["metrics"]
    hold_md, h = _holdings_md(winner["run_dir"], name_map)
    conf_ids = list(winner["confirm_ids"])
    lines += [
        "",
        f"## 结论：最优 = {winner_kind} / `{winner['label']}`",
        "",
        f"- excess={_pct(wm['excess_return'])}，sharpe={_num(wm['sharpe'])}，max_dd={_pct(wm['max_drawdown'])}",
        f"- 相对基线 171+054：Δexcess={_pct(float(wm['excess_return']) - base_ex)}",
        f"- 最近持仓日 {h['last_date']}；n={h['n_held']}；gross={_num(h['gross'])}",
        "",
        hold_md,
        "",
        f"- {sec.risk_note}",
        f"- 明细 CSV：`{csv_path}`",
    ]
    summary_path = sec.out_root / "confirm_search_summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Write optimal prop (frontend-ready).
    _write_optimal_prop(sec, winner, winner_kind, best_f0, f1_result, base_ex, irs, h, hold_md)
    print(f"wrote {summary_path}")
    print(f"wrote {sec.prop_out} (winner={winner_kind}/{winner['label']})")


def _write_optimal_prop(
    sec: Sector,
    winner: dict,
    winner_kind: str,
    best_f0: dict,
    f1_result: dict | None,
    base_ex: float,
    irs: dict[str, float],
    hold_stats: dict,
    hold_md: str,
) -> None:
    ids = list(winner["confirm_ids"])
    m = winner["metrics"]
    mc = (winner.get("validation") or {}).get("monte_carlo") or {}
    ir_weighted = ", ".join(
        f"{aid.replace('gtja191_', '')}:{float(irs.get(aid, 0.0)):.3f}" for aid in ids
    )
    id_rows = "\n".join(
        [f"| {MOM_ID} | 动量（主排序，必须） |"]
        + [f"| {aid} | 确认（本板块重搜硬确认） |" for aid in ids]
        + (
            ["| custom_max_inv | 极端彩票否决（仅底部 20%，不是主确认） |"]
            if winner_kind == "F1"
            else []
        )
    )
    copy_ids = ", ".join(
        [MOM_ID] + ids + (["custom_max_inv"] if winner_kind == "F1" else [])
    )
    d_ex_base = float(m["excess_return"]) - base_ex
    conf_txt = "+".join(a.replace("gtja191_", "") for a in ids) if ids else "无确认"

    f0m = best_f0["metrics"]
    f1_block = ""
    if f1_result is not None:
        f1m = f1_result["metrics"]
        d_ex = float(f1m["excess_return"]) - float(f0m["excess_return"])
        d_sh = float(f1m["sharpe"]) - float(f0m["sharpe"])
        f1_block = (
            f"同池 F0'(`{best_f0['label']}`) vs F1'：Δexcess={_pct(d_ex)}，Δsharpe={_num(d_sh)}。"
        )

    must = []
    if float(m["excess_return"]) < 0:
        must.append(
            f"- excess={_pct(m['excess_return'])}<0：未跑赢板块等权基准，必须明文承认。"
        )
    else:
        must.append(
            f"- excess={_pct(m['excess_return'])}>0：本样本相对板块基准为正，仍须样本外复核。"
        )
    p = mc.get("p_value_sharpe")
    if p is not None and float(p) > 0.2:
        must.append(f"- MC p_sharpe={_num(p)}>0.2。")
    else:
        must.append(f"- MC p_sharpe={_num(p)}（阈值 0.2）。")
    must.append(
        f"- 相对原基线 171/054：Δexcess={_pct(d_ex_base)}；确认腿为板块内存活因子重搜结果，有过拟合风险。"
    )

    if winner_kind == "F1":
        header = f"""请用国泰君安 191（gtja191）+ custom_max_inv 做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上，在 GTJA191 存活因子中自由组合重搜确认腿后的最优复现：
F1' = 动量 164 + 硬确认 {conf_txt} + custom_max_inv 底部否决。
{f1_block}
对照原基线 171/054：Δexcess={_pct(d_ex_base)}。
禁止为追收益擅自加反转/扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{chr(10).join(must)}"""
        n_core = 1 + len(ids) + 1
        filter_extra = """5. F1' 额外否决（仅新开仓）：smooth(custom_max_inv) < 当日截面 20% 分位 → 禁止新开
6. 面板只建 OHLCV；不要为 114 拼 amount"""
        ban = """【禁止】gtja191_114 / 反转因子；禁止用 custom 替换确认腿；
禁止 custom_ivol/abn_to/vp_corr/comp_low/near_low 进硬确认或重排"""
    else:
        header = f"""请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为{sec.name}板块（{sec.universe_desc}）上，在 GTJA191 存活因子中自由组合重搜确认腿后的最优复现：
F0' = 动量 164 + 硬确认 {conf_txt}。
{f1_block}
对照原基线 171/054：Δexcess={_pct(d_ex_base)}。
同池 F1' 未优于 F0' 时禁止叠加 custom 否决。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【必须承认（本样本）】
{chr(10).join(must)}"""
        n_core = 1 + len(ids)
        filter_extra = "5. 面板只建 OHLCV；不要为 114 拼 amount"
        ban = """【禁止】gtja191_114 / 反转因子；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排
【禁止】回退使用 171/054 作为本版确认腿（除非其完整引擎仍最优）"""

    body = f"""## 提示词

```
{header}

【股票池与区间】
- 标的池：A股{sec.name}相关板块（{sec.universe_desc}）
- 研究/回测参考区间：{sec.start_date} 至 {sec.end_date}（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 {n_core} 个）】
| ID | 角色 |
{id_rows}

复制用：{copy_ids}
{ban}

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权({ir_weighted if ids else '无'})；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
{filter_extra}

【持仓与换手（硬性）】
1. N=6 等权；保留带 Top9（N×1.5）
2. 调仓日：过确认后按 S 选 Top6；旧仓仍在 Top9 且未触发风控可续持
3. 换手硬顶 20%：优先留旧仓，再填新股；将超 20% 则停止换入（空仓建仓首期除外）
4. 最小持有 5 日：未满 5 日不因跌出保留带而卖；峰值回撤止损/止盈立即生效
5. 择时：板块等权均价 vs MA20，仅调仓日更新乘数（1.0/0.5）；非调仓日冻结

【退出】
峰值回撤≥15% → 止盈≥35% → 持仓≥20日且排名恶化≥10名 → 调仓日跌出 Top9（且满 5 日）
非调仓日不因排名换股。

【交付】
- 因子 ID + 角色；确认腿为本板块存活因子重搜结果
- 最近调仓日持仓表；完整指标 + MC p
- 必须对照原基线 171/054：Δtotal / Δexcess / Δsharpe
- 3–5 条白话：{sec.risk_note}
- 声明：研究/回测，非实盘建议

```

## 正式复跑摘要（自动写入）

| 项 | 值 |
|----|----|
| 最优 | **{winner_kind}** / `{winner['label']}` |
| excess / sharpe / max_dd | {_pct(m['excess_return'])} / {_num(m['sharpe'])} / {_pct(m['max_drawdown'])} |
| MC p_sharpe | {_num(mc.get('p_value_sharpe'))} |
| Δexcess vs 171+054 | {_pct(d_ex_base)} |
| 最近持仓日 | {hold_stats['last_date']}（n={hold_stats['n_held']}） |

{hold_md}

研究/回测，非实盘建议。
"""
    sec.prop_out.write_text(body, encoding="utf-8")


def _search_sector(
    sec: Sector,
    irs: dict[str, float],
    *,
    pair_pool: int,
    top_k: int,
    mc_sims: int,
    include_pure: bool,
) -> None:
    print("=" * 72)
    print(f"[sector] {sec.name} / {sec.panel}")
    sec.out_root.mkdir(parents=True, exist_ok=True)

    data_map = _load_ohlcv(sec.ohlcv_src)
    panel = _build_panel(data_map)
    close = panel["close"]
    print(f"[panel] symbols={close.shape[1]} bars={close.shape[0]}")

    alive_ids = sorted(irs)
    print("[precompute] factors…")
    scores = _precompute_scores(panel, alive_ids)
    if MOM_ID not in scores:
        raise SystemExit("mom factor failed")

    cands = [
        aid
        for aid in sorted(scores)
        if aid != MOM_ID and aid not in BANNED_CONFIRM
    ]
    print(f"[cands] confirm candidates={len(cands)} pair_pool={pair_pool}")

    cache: dict[str, dict] = {}
    rows: list[dict] = []

    # --- Stage 1: singles + baseline (+ pure) ---
    print("[stage1] singles + baseline…")
    stage1_combos: list[list[str]] = [list(BASELINE_CONFIRM)]
    if include_pure:
        stage1_combos.append([])
    for a in cands:
        stage1_combos.append([a])

    t0 = time.time()
    for i, ids in enumerate(stage1_combos, 1):
        row = _score_combo_cached(sec, ids, scores, irs, data_map, cache, mc_sims=0)
        rows.append(row)
        if i % 10 == 0 or i == len(stage1_combos):
            print(f"  stage1 {i}/{len(stage1_combos)} elapsed={time.time()-t0:.1f}s")

    singles = [r for r in rows if r["n_confirm"] == 1]
    singles.sort(key=lambda r: r["excess_return"], reverse=True)
    pool = [r["confirm_ids"][0] for r in singles[: max(pair_pool, 2)]]
    print(
        f"[stage1] best single={singles[0]['label'] if singles else 'n/a'} "
        f"excess={singles[0]['excess_return'] if singles else 'n/a'}; "
        f"pair pool n={len(pool)}"
    )

    # --- Stage 2: pairs among top singles ---
    print(f"[stage2] pairs among top-{len(pool)}…")
    seen = {tuple(r["confirm_ids"]) for r in rows}
    pair_list = list(itertools.combinations(pool, 2))
    for j, (a, b) in enumerate(pair_list, 1):
        key = (a, b)
        if key in seen or tuple(reversed(key)) in seen:
            continue
        if tuple(sorted(key)) == tuple(sorted(BASELINE_CONFIRM)):
            continue
        row = _score_combo_cached(sec, [a, b], scores, irs, data_map, cache, mc_sims=0)
        rows.append(row)
        if j % 10 == 0 or j == len(pair_list):
            print(f"  stage2 {j}/{len(pair_list)} elapsed={time.time()-t0:.1f}s")

    df = (
        pd.DataFrame(rows)
        .sort_values(["excess_return", "sharpe", "total_return"], ascending=False)
        .reset_index(drop=True)
    )
    print(
        f"[rank] n={len(df)} best={df.iloc[0]['label']} "
        f"excess={df.iloc[0]['excess_return']:.4f}"
    )

    # --- Stage 3: MC verify top-K + baseline + best single/pair ---
    verify_ids: list[list[str]] = []
    seen_lab: set[str] = set()

    def _add(ids: list[str]) -> None:
        lab = _combo_label(ids)
        if lab in seen_lab:
            return
        seen_lab.add(lab)
        verify_ids.append(ids)

    _add(list(BASELINE_CONFIRM))
    for _, row in df.head(max(top_k, 1)).iterrows():
        ids = row["confirm_ids"]
        if isinstance(ids, str):
            ids = list(ast.literal_eval(ids))
        _add(list(ids))
    best_single = df.loc[df["n_confirm"] == 1]
    best_pair = df.loc[df["n_confirm"] == 2]
    if len(best_single):
        ids = best_single.iloc[0]["confirm_ids"]
        if isinstance(ids, str):
            ids = list(ast.literal_eval(ids))
        _add(list(ids))
    if len(best_pair):
        ids = best_pair.iloc[0]["confirm_ids"]
        if isinstance(ids, str):
            ids = list(ast.literal_eval(ids))
        _add(list(ids))

    print(f"[stage3] MC verify {len(verify_ids)} combos (sims={mc_sims})…")
    verified: list[dict] = []
    for ids in verify_ids:
        lab = _combo_label(ids)
        run_dir = sec.out_root / f"full_{lab}"
        print(f"  → MC {lab}")
        result = _full_run(
            sec, ids, scores, irs, data_map, run_dir, mc_sims=mc_sims, veto_max=False
        )
        verified.append(result)
        print(
            f"    excess={result['metrics'].get('excess_return')} "
            f"mc_p={(result.get('validation') or {}).get('monte_carlo', {}).get('p_value_sharpe')}"
        )

    best_f0 = max(verified, key=lambda x: float(x["metrics"]["excess_return"]))
    print(
        f"[best F0] {best_f0['label']} excess={best_f0['metrics'].get('excess_return')}"
    )

    # --- Stage 4: F1 on best F0 confirm ---
    f1_result = None
    if ENGINE_F1_SRC.exists():
        print(f"[stage4] F1 MAX veto on best confirm {best_f0['label']}…")
        f1_dir = sec.out_root / f"full_{best_f0['label']}_F1"
        f1_result = _full_run(
            sec,
            list(best_f0["confirm_ids"]),
            scores,
            irs,
            data_map,
            f1_dir,
            mc_sims=mc_sims,
            veto_max=True,
        )
        # Keep confirm_ids / label for prop; mark veto.
        f1_result["confirm_ids"] = list(best_f0["confirm_ids"])
        f1_result["label"] = best_f0["label"]
        print(
            f"  F1 excess={f1_result['metrics'].get('excess_return')} "
            f"vs F0={best_f0['metrics'].get('excess_return')}"
        )
    else:
        print("[stage4] skip F1 — engine template missing")

    names = _name_map(sec)
    _write_outputs(sec, df, verified, f1_result, names, irs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--only",
        choices=(
            "robot",
            "memory",
            "foodbev",
            "compute",
            "drug",
            "speceq",
            "aiapp",
            "machine",
            "vrar",
            "autoparts",
        ),
        default=None,
    )
    ap.add_argument("--pair-pool", type=int, default=None, help="override per-sector default")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument("--include-pure", action="store_true", default=True)
    ap.add_argument("--no-pure", action="store_true")
    args = ap.parse_args()

    irs = _load_alive_irs()
    if MOM_ID not in irs:
        raise SystemExit(f"{MOM_ID} not in alive set")
    print(f"[survival] alive gtja191={len(irs)}")

    jobs = [s for s in SECTORS if args.only is None or s.key == args.only]
    for sec in jobs:
        pool = args.pair_pool if args.pair_pool is not None else sec.default_pair_pool
        _search_sector(
            sec,
            irs,
            pair_pool=pool,
            top_k=args.top_k,
            mc_sims=args.mc_sims,
            include_pure=(not args.no_pure),
        )
    print("[done]")


if __name__ == "__main__":
    main()
