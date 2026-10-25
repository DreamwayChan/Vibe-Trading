"""Tune F0 process knobs for 焦煤 BK1494 as a slow-bull / patient long book.

Baseline F0' (164+093/171) already beats EW on excess, but absolute return /
Sharpe are weak because MA20 half-cut, 15% peak-DD and 35% take-profit chop a
commodity slow trend. This script keeps the confirm legs fixed and searches
patient process variants (timing / stops / hold / N / turnover).

Primary rank: total_return, then sharpe, then excess (all must beat baseline
on total OR sharpe with excess still > 0 and max_dd not worse than -40%).

Run from agent/:
    python scripts/research_coking_slowbull_tune.py
    python scripts/research_coking_slowbull_tune.py --skip-search
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine  # noqa: E402
from scripts.research_copper_confirm_search import MOM_ID  # noqa: E402
from scripts.research_coking_confirm_search import (  # noqa: E402
    BK_CODE,
    BK_NAME,
    PANEL_LABEL,
    RISK_NOTE,
    UNIVERSE_DESC,
)
from scripts.research_sector_confirm_search import (  # noqa: E402
    ENGINE_F0_SRC,
    _build_panel,
    _load_alive_irs,
    _load_ohlcv,
    _name_map,
    _num,
    _pct,
    _smooth_cs,
    _write_signal_engine,
)
from src.factors.registry import Registry  # noqa: E402

UNIV_RUN = _AGENT_ROOT / "runs" / "20260827_083500_00_coking_universe"
BASE_F0 = _AGENT_ROOT / "runs" / "20260827_083500_03_F0_coking_093_171"
OUT_ROOT = _AGENT_ROOT / "runs" / "_research_coking_slowbull_tune"
PROP_OUT = _AGENT_ROOT / "prop_国泰君安_焦煤_最优.md"
RUN_PREFIX = "20260827_090400"
CONFIRM_IDS = ["gtja191_093", "gtja191_171"]
START_DATE = "2024-01-01"
END_DATE = "2026-08-26"
INITIAL_CASH = 1_000_000.0
MC_SIMS = 1000
MAX_DD_FLOOR = -0.40


@dataclass(frozen=True)
class Knobs:
    """Patient-long process knobs; defaults match F0' baseline."""

    name: str
    ma_period: int = 20
    timing_off: bool = False  # True → always fully invested (no MA half-cut)
    peak_dd_sl: float = 0.15
    tp: float = 0.35  # use >= 9.0 to disable take-profit
    min_hold_days: int = 5
    time_stop_days: int = 20  # use >= 999 to disable time/rank stop
    rank_worsen: int = 10
    top_n: int = 6
    band: int = 9
    max_turnover: float = 0.20
    confirm_q: float = 0.40
    # weekly (default) or monthly first trading day
    rebalance: str = "weekly"

    @property
    def tag(self) -> str:
        return self.name


# Focused slow-bull grid — keep confirm legs fixed; vary patience knobs only.
KNOB_GRID: list[Knobs] = [
    Knobs(name="baseline"),
    Knobs(name="no_timing", timing_off=True),
    Knobs(name="ma60", ma_period=60),
    Knobs(name="ma120", ma_period=120),
    Knobs(name="soft_stops", peak_dd_sl=0.25, tp=0.60, time_stop_days=40, min_hold_days=10),
    Knobs(
        name="patient",
        timing_off=True,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=15,
        max_turnover=0.10,
    ),
    Knobs(
        name="patient_ma60",
        ma_period=60,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=15,
        max_turnover=0.10,
    ),
    Knobs(
        name="patient_top4",
        timing_off=True,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=15,
        top_n=4,
        band=6,
        max_turnover=0.10,
    ),
    Knobs(
        name="ride",
        timing_off=True,
        peak_dd_sl=0.35,
        tp=9.99,
        time_stop_days=999,
        min_hold_days=20,
        max_turnover=0.10,
    ),
    Knobs(
        name="ride_ma60",
        ma_period=60,
        peak_dd_sl=0.35,
        tp=9.99,
        time_stop_days=999,
        min_hold_days=20,
        max_turnover=0.10,
    ),
    Knobs(
        name="monthly_patient",
        timing_off=True,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=20,
        max_turnover=0.15,
        rebalance="monthly",
    ),
    Knobs(
        name="monthly_ma60",
        ma_period=60,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=20,
        max_turnover=0.15,
        rebalance="monthly",
    ),
    Knobs(
        name="confirm30_patient",
        timing_off=True,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=15,
        confirm_q=0.30,
        max_turnover=0.10,
    ),
    Knobs(
        name="confirm50_patient",
        timing_off=True,
        peak_dd_sl=0.30,
        tp=9.99,
        time_stop_days=60,
        min_hold_days=15,
        confirm_q=0.50,
        max_turnover=0.10,
    ),
    Knobs(
        name="top4_soft",
        ma_period=60,
        peak_dd_sl=0.25,
        tp=0.80,
        time_stop_days=40,
        min_hold_days=10,
        top_n=4,
        band=6,
        max_turnover=0.15,
    ),
]


def _confirm_ok(
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    close: pd.DataFrame,
    confirm_q: float,
) -> pd.DataFrame:
    conf_num = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    conf_den = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for aid in CONFIRM_IDS:
        sm = scores.get(aid)
        if sm is None:
            continue
        w = max(float(irs.get(aid, 0.10)), 0.0)
        valid = sm.notna()
        conf_num = conf_num.add(sm.fillna(0.0) * w, fill_value=0.0)
        conf_den = conf_den.add(valid.astype(float) * w, fill_value=0.0)
    conf = conf_num.divide(conf_den.replace(0.0, np.nan))
    thr = conf.quantile(confirm_q, axis=1)
    ok = conf.ge(thr, axis=0).fillna(False)
    ok.loc[conf.isna().all(axis=1), :] = True
    return ok


def _rebalance_mask(dates: pd.DatetimeIndex, mode: str) -> np.ndarray:
    d = pd.DatetimeIndex(dates)
    if mode == "monthly":
        month_id = pd.Series(d.strftime("%Y-%m"), index=d)
        return (~month_id.duplicated(keep="first")).to_numpy()
    iso = d.isocalendar()
    week_id = pd.Series(iso["year"].astype(str) + "-" + iso["week"].astype(str), index=d)
    return (~week_id.duplicated(keep="first")).to_numpy()


def _exit_reason(code, px, rank_row, entry_px, peak_px, entry_rank, hold_days, check_band, k: Knobs):
    cost = entry_px.get(code)
    peak = peak_px.get(code, px)
    if cost is None or cost <= 0:
        return None
    days = hold_days.get(code, 0)
    if peak > 0 and (peak - px) / peak >= k.peak_dd_sl:
        return "peak_dd"
    if k.tp < 9.0 and px / cost - 1.0 >= k.tp:
        return "take_profit"
    if days >= k.time_stop_days and k.time_stop_days < 900:
        er = entry_rank.get(code)
        cr = rank_row.get(code, np.nan)
        if er is not None and pd.notna(cr) and float(cr) - float(er) >= k.rank_worsen:
            return "time_stop"
    if check_band and days >= k.min_hold_days:
        cr = rank_row.get(code, np.nan)
        if pd.isna(cr) or float(cr) > k.band:
            return "band_exit"
    return None


def _select(rank_row, conf_row, holdings, entry_px, peak_px, entry_rank, hold_days, close, i, prev_w, timing_scale, k: Knobs):
    must_exit = set()
    keep_candidates = []
    for c in sorted(holdings):
        px = float(close.iloc[i][c])
        if pd.isna(px):
            must_exit.add(c)
            continue
        if _exit_reason(c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, True, k) is not None:
            must_exit.add(c)
            continue
        keep_candidates.append(c)

    ranked = rank_row.dropna().sort_values()
    new_pool = []
    for c in ranked.index:
        if c in must_exit or c in keep_candidates:
            continue
        try:
            passed = bool(conf_row[c])
        except Exception:
            passed = False
        if not passed:
            continue
        if float(ranked[c]) <= float(k.band):
            new_pool.append(c)

    selected = list(keep_candidates)
    prev_gross = float(prev_w.abs().sum())

    def _turnover(sel):
        target = {code: timing_scale / len(sel) for code in sel} if sel else {}
        keys = set(prev_w.index) | set(target)
        return 0.5 * sum(abs(float(target.get(x, 0.0)) - float(prev_w.get(x, 0.0))) for x in keys)

    apply_cap = prev_gross > 1e-6
    for c in new_pool:
        if len(selected) >= k.top_n:
            break
        trial = selected + [c]
        if (not apply_cap) or _turnover(trial) <= k.max_turnover + 1e-9:
            selected.append(c)
    if len(selected) > k.top_n:
        selected = sorted(selected, key=lambda x: float(rank_row.get(x, 1e9)))[: k.top_n]
    if not selected:
        selected = list(new_pool[: k.top_n])
    return set(selected)


def _build_weights(sort_score: pd.DataFrame, confirm_ok: pd.DataFrame, close: pd.DataFrame, k: Knobs):
    rank_df = sort_score.rank(axis=1, ascending=False, method="first")
    dates = close.index
    is_reb = _rebalance_mask(dates, k.rebalance)
    n_reb = int(np.asarray(is_reb).sum())
    sector_close = close.mean(axis=1)
    sector_ma = sector_close.rolling(k.ma_period, min_periods=5).mean()
    weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
    holdings: set[str] = set()
    entry_px: dict = {}
    peak_px: dict = {}
    entry_rank: dict = {}
    hold_days: dict = {}
    prev_w = pd.Series(0.0, index=weights.columns)
    timing_scale = 1.0

    for i in range(len(dates)):
        rank_row = rank_df.iloc[i]
        conf_row = confirm_ok.iloc[i]
        current = pd.Series(0.0, index=weights.columns)
        if is_reb[i]:
            if k.timing_off:
                timing_scale = 1.0
            else:
                sc = sector_close.iloc[i]
                sma = sector_ma.iloc[i]
                timing_scale = (
                    0.5 if (pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma)) else 1.0
                )
            selected = _select(
                rank_row, conf_row, holdings, entry_px, peak_px, entry_rank, hold_days,
                close, i, prev_w, timing_scale, k,
            )
            n_sel = len(selected)
            if n_sel > 0:
                wv = timing_scale / n_sel
                for c in selected:
                    current[c] = wv
                    px = float(close.iloc[i][c])
                    if c not in holdings:
                        entry_px[c] = px
                        peak_px[c] = px
                        entry_rank[c] = float(rank_row[c]) if pd.notna(rank_row[c]) else k.band
                        hold_days[c] = 0
                    else:
                        peak_px[c] = max(peak_px.get(c, px), px)
                        hold_days[c] = hold_days.get(c, 0) + 1
            for c in holdings - selected:
                entry_px.pop(c, None)
                peak_px.pop(c, None)
                entry_rank.pop(c, None)
                hold_days.pop(c, None)
            holdings = selected
            prev_w = current
        else:
            survivors = set()
            for c in sorted(holdings):
                if prev_w.get(c, 0.0) <= 0:
                    continue
                px = float(close.iloc[i][c])
                if pd.isna(px):
                    continue
                peak_px[c] = max(peak_px.get(c, px), px)
                hold_days[c] = hold_days.get(c, 0) + 1
                if _exit_reason(c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, False, k) is not None:
                    continue
                survivors.add(c)
                current[c] = float(prev_w[c])
            for c in holdings - survivors:
                entry_px.pop(c, None)
                peak_px.pop(c, None)
                entry_rank.pop(c, None)
                hold_days.pop(c, None)
            holdings = survivors
            prev_w = current
        weights.iloc[i] = current
    return weights, n_reb


class _KnobEngine:
    """SignalEngine wrapper that injects slow-bull knobs into weight building."""

    def __init__(self, scores, irs, knobs: Knobs):
        self.mom_id = MOM_ID
        self.confirm_ids = list(CONFIRM_IDS)
        self.alpha_ids = [self.mom_id] + self.confirm_ids
        self.scores = scores
        self.irs = irs
        self.knobs = knobs
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).sort_index()
        mom = self.scores[self.mom_id].reindex(index=close.index, columns=close.columns)
        ok = _confirm_ok(self.scores, self.irs, close, self.knobs.confirm_q)
        weights, n_reb = _build_weights(mom, ok, close, self.knobs)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _score_row(metrics: dict, n_reb: int, name: str) -> dict:
    return {
        "name": name,
        "total_return": float(metrics.get("total_return") or 0.0),
        "excess_return": float(metrics.get("excess_return") or 0.0),
        "sharpe": float(metrics.get("sharpe") or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown") or 0.0),
        "annual_return": float(metrics.get("annual_return") or 0.0),
        "trade_count": float(metrics.get("trade_count") or 0.0),
        "avg_holding_days": float(metrics.get("avg_holding_days") or 0.0),
        "rebalance_turnover_mean": float(metrics.get("rebalance_turnover_mean") or 0.0),
        "is_rebalance_days": n_reb,
    }


def _run_variant(data_map, scores, irs, knobs: Knobs, run_dir: Path, mc_sims: int) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    cfg = {
        "codes": sorted(data_map),
        "source": "tushare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": INITIAL_CASH,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": [MOM_ID] + CONFIRM_IDS,
        "parameters": {
            "strategy": f"coking_slowbull_{knobs.tag}",
            "panel": PANEL_LABEL,
            "confirm_ids": CONFIRM_IDS,
            "knobs": asdict(knobs),
        },
        "validation": {"monte_carlo": {"n_simulations": mc_sims}},
        "_run_card_effective_sources": ["local_artifacts"],
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sig = _KnobEngine(scores, irs, knobs)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    row = _score_row(metrics, sig.last_is_rebalance_days, knobs.tag)
    row["validation"] = validation
    row["run_dir"] = str(run_dir)
    row["knobs"] = asdict(knobs)
    return row


def _composite(row: dict) -> tuple:
    """Prefer absolute return + Sharpe for slow-bull; keep excess as tie-break."""
    return (row["total_return"], row["sharpe"], row["excess_return"], -abs(row["max_drawdown"]))


def _eligible(row: dict, baseline: dict) -> bool:
    if row["max_drawdown"] < MAX_DD_FLOOR:
        return False
    if row["excess_return"] <= 0:
        return False
    # Must improve absolute return or Sharpe vs baseline F0'.
    return (row["total_return"] > baseline["total_return"] + 1e-6) or (
        row["sharpe"] > baseline["sharpe"] + 1e-6 and row["total_return"] >= baseline["total_return"] - 0.01
    )


def _patch_engine_file(dst: Path, irs: dict[str, float], knobs: Knobs) -> None:
    """Write a loadable SignalEngine with confirm legs + slow-bull knobs baked in."""
    _write_signal_engine(dst, CONFIRM_IDS, irs, veto_max=False)
    text = dst.read_text(encoding="utf-8")

    # Replace F0 default process constants in __init__.
    replacements = {
        r"self\.smooth_w = 5": "self.smooth_w = 5",
        r"self\.top_n = \d+": f"self.top_n = {knobs.top_n}",
        r"self\.band = \d+": f"self.band = {knobs.band}",
        r"self\.max_turnover = [0-9.]+": f"self.max_turnover = {knobs.max_turnover}",
        r"self\.min_hold_days = \d+": f"self.min_hold_days = {knobs.min_hold_days}",
        r"self\.confirm_q = [0-9.]+": f"self.confirm_q = {knobs.confirm_q}",
        r"self\.peak_dd_sl = [0-9.]+": f"self.peak_dd_sl = {knobs.peak_dd_sl}",
        r"self\.tp = [0-9.]+": f"self.tp = {knobs.tp}",
        r"self\.time_stop_days = \d+": f"self.time_stop_days = {knobs.time_stop_days}",
        r"self\.rank_worsen = \d+": f"self.rank_worsen = {knobs.rank_worsen}",
        r"self\.ma_period = \d+": f"self.ma_period = {knobs.ma_period}",
    }
    for pat, repl in replacements.items():
        text, n = re.subn(pat, repl, text, count=1)
        if n != 1:
            raise SystemExit(f"failed to patch engine field: {pat}")

    # Inject timing_off + rebalance mode after ma_period.
    inject = (
        f"        self.ma_period = {knobs.ma_period}\n"
        f"        self.timing_off = {knobs.timing_off}\n"
        f"        self.rebalance_mode = {knobs.rebalance!r}"
    )
    text, n = re.subn(rf"        self\.ma_period = {knobs.ma_period}", inject, text, count=1)
    if n != 1:
        raise SystemExit("failed to inject timing_off / rebalance_mode")

    # Patch timing branch to honor timing_off.
    old_timing = (
        "            if is_reb[i]:\n"
        "                sc = sector_close.iloc[i]; sma = sector_ma.iloc[i]\n"
        "                timing_scale = 0.5 if (pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma)) else 1.0"
    )
    new_timing = (
        "            if is_reb[i]:\n"
        "                if getattr(self, 'timing_off', False):\n"
        "                    timing_scale = 1.0\n"
        "                else:\n"
        "                    sc = sector_close.iloc[i]; sma = sector_ma.iloc[i]\n"
        "                    timing_scale = 0.5 if (pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma)) else 1.0"
    )
    if old_timing not in text:
        raise SystemExit("engine timing branch drift — cannot patch timing_off")
    text = text.replace(old_timing, new_timing, 1)

    # Disable take-profit when tp is a sentinel.
    if knobs.tp >= 9.0:
        text = text.replace(
            "        if px / cost - 1.0 >= self.tp:\n            return \"take_profit\"",
            "        if self.tp < 9.0 and px / cost - 1.0 >= self.tp:\n            return \"take_profit\"",
            1,
        )

    # Disable time-stop when sentinel.
    if knobs.time_stop_days >= 900:
        text = text.replace(
            "        if days >= self.time_stop_days:",
            "        if days >= self.time_stop_days and self.time_stop_days < 900:",
            1,
        )

    # Monthly rebalance override.
    if knobs.rebalance == "monthly":
        old_reb = (
            "    def _rebalance_mask(self, dates):\n"
            "        d = pd.DatetimeIndex(dates)\n"
            "        iso = d.isocalendar()\n"
            "        week_id = pd.Series(iso[\"year\"].astype(str) + \"-\" + iso[\"week\"].astype(str), index=d)\n"
            "        return (~week_id.duplicated(keep=\"first\")).to_numpy()"
        )
        new_reb = (
            "    def _rebalance_mask(self, dates):\n"
            "        d = pd.DatetimeIndex(dates)\n"
            "        if getattr(self, 'rebalance_mode', 'weekly') == 'monthly':\n"
            "            month_id = pd.Series(d.strftime('%Y-%m'), index=d)\n"
            "            return (~month_id.duplicated(keep='first')).to_numpy()\n"
            "        iso = d.isocalendar()\n"
            "        week_id = pd.Series(iso[\"year\"].astype(str) + \"-\" + iso[\"week\"].astype(str), index=d)\n"
            "        return (~week_id.duplicated(keep=\"first\")).to_numpy()"
        )
        if old_reb not in text:
            raise SystemExit("engine rebalance mask drift")
        text = text.replace(old_reb, new_reb, 1)

    dst.write_text(text, encoding="utf-8")


def _holdings_md(run_dir: Path, names: dict[str, str]) -> tuple[str, dict]:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
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
        lines.append(f"| {i} | {code} | {names.get(code, '')} | {float(w):.4f} |")
    return "\n".join(lines), stats


def publish(best: dict, baseline: dict, irs: dict[str, float], data_map: dict, names: dict) -> Path:
    knobs = Knobs(**best["knobs"])
    tag = knobs.tag
    dst = _AGENT_ROOT / "runs" / f"{RUN_PREFIX}_05_F0_coking_slowbull_{tag}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(Path(best["run_dir"]), dst)

    # Stamp OHLCV for frontend charts + rewrite a loadable engine with knobs.
    art = dst / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    for code, frame in data_map.items():
        out = frame.copy()
        out.index.name = "trade_date"
        out.to_csv(art / f"ohlcv_{code}.csv")
    _patch_engine_file(dst / "code" / "signal_engine.py", irs, knobs)

    cfg = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    cfg["parameters"]["engine"] = f"F0_slowbull_{tag}"
    cfg["parameters"]["universe"] = UNIVERSE_DESC
    cfg["parameters"]["note"] = (
        "slow-bull process retune on fixed confirm 093/171; "
        "optimize total_return/sharpe vs baseline F0'"
    )
    (dst / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    hold_md, hstat = _holdings_md(dst, names)
    v = best.get("validation") or {}
    mc = v.get("monte_carlo") or v
    p_sh = mc.get("p_value_sharpe")

    d_tot = best["total_return"] - baseline["total_return"]
    d_ex = best["excess_return"] - baseline["excess_return"]
    d_sh = best["sharpe"] - baseline["sharpe"]

    report = f"""# F0' 慢牛过程优化报告 — 焦煤 BK1494（{tag}）

**声明：研究/回测结果，非实盘建议。确认腿仍为 093/171；本版只改过程参数以适配慢牛长持。**

## 1. 优化动机

基线 F0'（周频 / MA20 半仓 / 15%峰值回撤 / 35%止盈）在焦煤上 excess 尚可，但绝对收益与夏普偏弱。
板块定性为可持续做多慢牛时，短 MA 择时与紧止盈/止损容易砍掉趋势腿。

## 2. 过程变更（相对基线）

| 项 | 基线 | 本版 `{tag}` |
|----|------|-------------|
| 确认腿 | 093+171 | 093+171（不变） |
| 择时 | MA20 半仓 | {"关闭（始终满仓尺度）" if knobs.timing_off else f"MA{knobs.ma_period} 半仓"} |
| 峰值回撤止损 | 15% | {knobs.peak_dd_sl:.0%} |
| 止盈 | 35% | {"关闭" if knobs.tp >= 9 else f"{knobs.tp:.0%}"} |
| 时间/排名止损 | 20日 | {"关闭" if knobs.time_stop_days >= 900 else f"{knobs.time_stop_days}日"} |
| 最小持有 | 5日 | {knobs.min_hold_days}日 |
| N / 保留带 | 6 / 9 | {knobs.top_n} / {knobs.band} |
| 换手帽 | 20% | {knobs.max_turnover:.0%} |
| 确认分位 | 40% | {knobs.confirm_q:.0%} |
| 调仓 | 周频 | {knobs.rebalance} |

## 3. 绩效对照

| 指标 | 慢牛优化 `{tag}` | 基线 F0' | Δ |
|------|------------------|----------|---|
| total_return | {_pct(best['total_return'])} | {_pct(baseline['total_return'])} | {_pct(d_tot)} |
| excess_return | {_pct(best['excess_return'])} | {_pct(baseline['excess_return'])} | {_pct(d_ex)} |
| sharpe | {_num(best['sharpe'])} | {_num(baseline['sharpe'])} | {_num(d_sh)} |
| max_drawdown | {_pct(best['max_drawdown'])} | {_pct(baseline['max_drawdown'])} | — |
| avg_holding_days | {_num(best['avg_holding_days'], 1)} | {_num(baseline['avg_holding_days'], 1)} | — |
| rebalance_turnover_mean | {_num(best['rebalance_turnover_mean'])} | {_num(baseline['rebalance_turnover_mean'])} | — |
| is_rebalance_days | {best['is_rebalance_days']} | {baseline['is_rebalance_days']} | — |
| MC p_sharpe | {_num(p_sh)} | 0.5220 | — |

### 必须承认

- excess={_pct(best['excess_return'])}；样本内过程重搜有过拟合风险。
- MC p_sharpe={_num(p_sh)}（阈值 0.2）。
- {RISK_NOTE}。

## 4. 最近持仓

- 日期：{hstat['last_date']}；持仓 {hstat['n_held']} 只；权重和 {_num(hstat['gross'])}

{hold_md}

## 5. 白话结论

1. 焦煤按慢牛处理：拉长持有、放宽止盈/止损，减少 MA 半仓对趋势的打断。
2. 因子确认腿不扩容（仍 164+093/171），只改过程，避免「换因子追收益」。
3. 样本内绝对收益/夏普相对基线提升，不代表样本外可持续。
4. 研究/回测，非实盘建议。

---
run: `{dst.name}`
"""
    (dst / f"REPORT_F0_coking_slowbull_{tag}.md").write_text(report, encoding="utf-8")
    (dst / "req.json").write_text(
        json.dumps(
            {
                "title": f"F0'慢牛优化-焦煤-{tag}",
                "prompt": (
                    f"请用国泰君安191做焦煤慢牛过程优化回测：BK1494，"
                    f"动量164+确认093/171，过程={tag}"
                ),
                "context": {
                    "panel": PANEL_LABEL,
                    "confirm_ids": CONFIRM_IDS,
                    "knobs": asdict(knobs),
                    "baseline_run": BASE_F0.name,
                    "ohlcv_from": UNIV_RUN.name,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Refresh prop summary to point at the slow-bull winner.
    prop = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行。

【背景与本版目标】
本版为焦煤板块（{UNIVERSE_DESC}）慢牛过程优化：
确认腿沿用 F0' 重搜结果 093/171；过程改为 `{tag}`（相对周频/MA20/紧止盈止损的基线）。
优化看 total_return 与 sharpe；对照基线 Δtotal={_pct(d_tot)}，Δsharpe={_num(d_sh)}。

【必须承认（本样本）】
- total={_pct(best['total_return'])}，excess={_pct(best['excess_return'])}，sharpe={_num(best['sharpe'])}
- MC p_sharpe={_num(p_sh)}
- 过程参数样本内重搜有过拟合风险；焦煤价格周期/钢厂需求扰动

【股票池与区间】
- 标的池：A股焦煤（{UNIVERSE_DESC}）
- 区间：{START_DATE} 至 {END_DATE}
- 因子：gtja191_164, gtja191_093, gtja191_171
- 过程：{json.dumps(asdict(knobs), ensure_ascii=False)}

【交付】
- 前端 run：`{dst.name}`
- 对照基线：`{BASE_F0.name}`
- 声明：研究/回测，非实盘建议
```

## 正式复跑摘要

| 项 | 值 |
|----|----|
| 最优 | **慢牛过程 `{tag}`** / 确认 `093+171` |
| 优化 run | `{dst.name}` |
| 基线 F0' | `{BASE_F0.name}` |
| total / excess / sharpe / max_dd | {_pct(best['total_return'])} / {_pct(best['excess_return'])} / {_num(best['sharpe'])} / {_pct(best['max_drawdown'])} |
| Δtotal / Δsharpe vs 基线 | {_pct(d_tot)} / {_num(d_sh)} |
| MC p_sharpe | {_num(p_sh)} |
| 最近持仓日 | {hstat['last_date']}（n={hstat['n_held']}） |

**声明：研究/回测结果，非实盘建议。**
"""
    PROP_OUT.write_text(prop, encoding="utf-8")
    print(f"[publish] {dst.name}")
    return dst


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--mc-sims", type=int, default=MC_SIMS)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    if not UNIV_RUN.exists():
        raise SystemExit(f"missing universe: {UNIV_RUN}")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    data_map = _load_ohlcv(UNIV_RUN)
    panel = _build_panel(data_map)
    irs = _load_alive_irs()
    for aid in [MOM_ID] + CONFIRM_IDS:
        irs.setdefault(aid, 0.10)

    # Reuse factor scores once for the whole grid.
    need = [MOM_ID] + CONFIRM_IDS
    scores: dict[str, pd.DataFrame] = {}
    reg = Registry()
    for aid in need:
        raw = reg.compute(aid, panel)
        if raw is None or raw.empty:
            raise SystemExit(f"factor empty: {aid}")
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = _smooth_cs(raw)
        print(f"[factor] {aid} non_null={int(scores[aid].notna().to_numpy().sum())}")

    rank_csv = OUT_ROOT / "slowbull_rank.csv"
    rows: list[dict] = []
    if args.skip_search and rank_csv.exists():
        # Reload prior search metrics; still need run dirs for top-K MC/publish.
        prev = pd.read_csv(rank_csv)
        print(f"[skip-search] loaded {len(prev)} rows")
        for _, r in prev.iterrows():
            kn = next((x for x in KNOB_GRID if x.name == r["name"]), None)
            if kn is None:
                continue
            run_dir = OUT_ROOT / f"fast_{kn.name}"
            if not (run_dir / "artifacts" / "metrics.csv").exists():
                continue
            metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
            row = _score_row(metrics, int(r.get("is_rebalance_days") or 0), kn.name)
            row["run_dir"] = str(run_dir)
            row["knobs"] = asdict(kn)
            rows.append(row)
    else:
        t0 = time.time()
        for i, kn in enumerate(KNOB_GRID, 1):
            run_dir = OUT_ROOT / f"fast_{kn.name}"
            print(f"[{i}/{len(KNOB_GRID)}] {kn.name} …")
            row = _run_variant(data_map, scores, irs, kn, run_dir, mc_sims=0)
            rows.append(row)
            print(
                f"  total={row['total_return']:.4f} excess={row['excess_return']:.4f} "
                f"sharpe={row['sharpe']:.4f} maxdd={row['max_drawdown']:.4f}"
            )
        print(f"[grid] done {time.time() - t0:.1f}s")

    df = pd.DataFrame(
        [
            {
                k: r[k]
                for k in (
                    "name",
                    "total_return",
                    "excess_return",
                    "sharpe",
                    "max_drawdown",
                    "annual_return",
                    "trade_count",
                    "avg_holding_days",
                    "rebalance_turnover_mean",
                    "is_rebalance_days",
                )
            }
            for r in rows
        ]
    ).sort_values(["total_return", "sharpe", "excess_return"], ascending=False)
    df.to_csv(rank_csv, index=False, encoding="utf-8-sig")
    print(df.to_string(index=False))

    baseline = next(r for r in rows if r["name"] == "baseline")
    # Prefer eligible improvers; fall back to best composite if none pass.
    candidates = [r for r in rows if _eligible(r, baseline)]
    if not candidates:
        print("[warn] no eligible improver — falling back to best composite")
        candidates = list(rows)
    candidates.sort(key=_composite, reverse=True)

    # MC verify top-K eligible, then pick best verified.
    verified: list[dict] = []
    for r in candidates[: max(args.top_k, 1)]:
        kn = Knobs(**r["knobs"])
        if kn.name == "baseline":
            # Attach baseline MC from published F0 if present.
            val_path = BASE_F0 / "artifacts" / "validation.json"
            if val_path.exists():
                r = dict(r)
                r["validation"] = json.loads(val_path.read_text(encoding="utf-8"))
            verified.append(r)
            continue
        print(f"[mc] {kn.name} sims={args.mc_sims}")
        full = _run_variant(
            data_map, scores, irs, kn, OUT_ROOT / f"full_{kn.name}", mc_sims=args.mc_sims
        )
        verified.append(full)

    verified.sort(key=_composite, reverse=True)
    best = verified[0]
    if best["name"] == "baseline":
        # If baseline still wins after MC, publish nothing new — keep prior F0'.
        print("[result] baseline still best after slow-bull search")
        (OUT_ROOT / "winner.json").write_text(
            json.dumps({"winner": "baseline", "note": "no slow-bull improver"}, indent=2),
            encoding="utf-8",
        )
        return

    names = {}
    meta = UNIV_RUN / "universe_meta.json"
    if meta.exists():
        names = {
            str(k): str(v)
            for k, v in (json.loads(meta.read_text(encoding="utf-8")).get("names") or {}).items()
        }
    dst = publish(best, baseline, irs, data_map, names)
    (OUT_ROOT / "winner.json").write_text(
        json.dumps(
            {
                "winner": best["name"],
                "run": dst.name,
                "total_return": best["total_return"],
                "excess_return": best["excess_return"],
                "sharpe": best["sharpe"],
                "knobs": best["knobs"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("[done]")


if __name__ == "__main__":
    main()
