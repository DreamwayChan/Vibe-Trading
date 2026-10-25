"""Search confirm-leg combos among GTJA191 alive factors on 金属铜 BK1615.

Process constraints match copper F0:
  - mom sort = gtja191_164 (fixed)
  - hard confirm @ CS 40%, IR-weighted when 2 legs
  - weekly first-trading-day rebalance, turnover cap 20%, band Top9,
    min hold 5d, peak-DD / TP / time-stop, sector MA20 timing

Search space (default):
  - singles + pairs among alive factors excluding mom and banned 114
  - baseline F0 = (171, 054) always included
  - optional pure_164 (no confirm) control

Fast path precomputes smooth CS ranks once, scores combos in-memory,
then re-runs Top-K + baseline through the real market engine (+ MC).

Run from agent/:
    python scripts/research_copper_confirm_search.py
    python scripts/research_copper_confirm_search.py --top-k 8 --max-pair 2
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year, calc_metrics  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine  # noqa: E402
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402

SRC_RUN = _AGENT_ROOT / "runs" / "20260726_131500_00_copper_universe"
OUT_ROOT = _AGENT_ROOT / "runs" / "_research_copper_confirm_search"
PROP_OUT = _AGENT_ROOT / "prop_国泰君安_金属铜_确认腿搜索.md"
REPORT_OUT = OUT_ROOT / "confirm_search_summary.md"

MOM_ID = "gtja191_164"
# Copper prompt bans reverse 114 from hard confirm / rerank.
BANNED_CONFIRM = frozenset({"gtja191_114"})
BASELINE_CONFIRM = ("gtja191_171", "gtja191_054")
FALLBACK_IR = {
    "gtja191_164": 0.145,
    "gtja191_171": 0.155,
    "gtja191_054": 0.152,
}

# Same process knobs as F0 copper / research_custom_positive.
SMOOTH_W = 5
TOP_N = 6
BAND = 9
MAX_TURNOVER = 0.20
MIN_HOLD_DAYS = 5
CONFIRM_Q = 0.40
PEAK_DD_SL = 0.15
TP = 0.35
TIME_STOP_DAYS = 20
RANK_WORSEN = 10
MA_PERIOD = 20

# Lightweight cost model for ranking only (full engine validates Top-K).
COST_ONE_WAY = 0.0003 + 0.0005  # commission + slippage; stamp on sells ~handled via 1.5x
COST_SELL_EXTRA = 0.001


def _load_alive_irs() -> dict[str, float]:
    """Load gtja191 alive IRs from the survival store."""
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


def _load_ohlcv() -> dict[str, pd.DataFrame]:
    art = SRC_RUN / "artifacts"
    if not art.exists():
        raise SystemExit(f"missing copper OHLCV cache: {art}")
    data_map: dict[str, pd.DataFrame] = {}
    for path in sorted(art.glob("ohlcv_*.csv")):
        code = path.stem.replace("ohlcv_", "")
        df = pd.read_csv(path, parse_dates=["trade_date"]).set_index("trade_date").sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        if len(df) < 120 or "close" not in keep:
            continue
        data_map[code] = df[keep].copy()
    if len(data_map) < 8:
        raise SystemExit(f"too few OHLCV symbols: {len(data_map)}")
    return data_map


def _build_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """OHLCV panel + synthetic amount=close*volume for amount-dependent alives.

    Copper F0 still bans gtja191_114 as a confirm leg; synthetic amount only
    unlocks other amount-gated survivors (007/012/…), not the reverse factor.
    """
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


def _smooth_cs(raw: pd.DataFrame) -> pd.DataFrame:
    cs = raw.rank(axis=1, pct=True, ascending=True)
    return cs.rolling(window=SMOOTH_W, min_periods=3).mean()


def _precompute_scores(
    panel: dict[str, pd.DataFrame], alive_ids: list[str]
) -> dict[str, pd.DataFrame]:
    """Compute 5d-smoothed CS percentile once per alive factor."""
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
        # Align to panel close index/columns so combo math stays rectangular.
        raw = raw.reindex(index=panel["close"].index, columns=panel["close"].columns)
        scores[aid] = _smooth_cs(raw)
        nn = int(scores[aid].notna().to_numpy().sum())
        print(f"  factor {aid}: non_null={nn} ({time.time() - t0:.2f}s)")
    return scores


def _rebalance_mask(dates: pd.DatetimeIndex) -> np.ndarray:
    iso = dates.isocalendar()
    week_id = pd.Series(
        iso["year"].astype(str) + "-" + iso["week"].astype(str), index=dates
    )
    return (~week_id.duplicated(keep="first")).to_numpy()


def _confirm_ok(
    confirm_ids: tuple[str, ...],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    close: pd.DataFrame,
) -> pd.DataFrame:
    """IR-weighted hard confirm mask; all-missing days pass everyone."""
    if not confirm_ids:
        return pd.DataFrame(True, index=close.index, columns=close.columns)

    conf_num = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    conf_den = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    active = 0
    for aid in confirm_ids:
        sm = scores.get(aid)
        if sm is None:
            continue
        w = max(float(irs.get(aid, 0.10)), 0.0)
        valid = sm.notna()
        conf_num = conf_num.add(sm.fillna(0.0) * w, fill_value=0.0)
        conf_den = conf_den.add(valid.astype(float) * w, fill_value=0.0)
        active += 1
    if active == 0:
        return pd.DataFrame(True, index=close.index, columns=close.columns)

    conf = conf_num.divide(conf_den.replace(0.0, np.nan))
    thr = conf.quantile(CONFIRM_Q, axis=1)
    ok = conf.ge(thr, axis=0).fillna(False)
    ok.loc[conf.isna().all(axis=1), :] = True
    return ok


def _exit_reason(
    code,
    px,
    rank_row,
    entry_px,
    peak_px,
    entry_rank,
    hold_days,
    check_band,
):
    cost = entry_px.get(code)
    peak = peak_px.get(code, px)
    if cost is None or cost <= 0:
        return None
    days = hold_days.get(code, 0)
    if peak > 0 and (peak - px) / peak >= PEAK_DD_SL:
        return "peak_dd"
    if px / cost - 1.0 >= TP:
        return "take_profit"
    if days >= TIME_STOP_DAYS:
        er = entry_rank.get(code)
        cr = rank_row.get(code, np.nan)
        if er is not None and pd.notna(cr) and float(cr) - float(er) >= RANK_WORSEN:
            return "time_stop"
    if check_band and days >= MIN_HOLD_DAYS:
        cr = rank_row.get(code, np.nan)
        if pd.isna(cr) or float(cr) > BAND:
            return "band_exit"
    return None


def _select_with_turnover_cap(
    rank_row,
    conf_row,
    holdings,
    entry_px,
    peak_px,
    entry_rank,
    hold_days,
    close,
    i,
    prev_w,
    timing_scale,
):
    must_exit = set()
    keep_candidates = []
    for c in sorted(holdings):
        px = float(close.iloc[i][c])
        if pd.isna(px):
            must_exit.add(c)
            continue
        if (
            _exit_reason(
                c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, True
            )
            is not None
        ):
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
        if float(ranked[c]) <= float(BAND):
            new_pool.append(c)

    selected = list(keep_candidates)
    prev_gross = float(prev_w.abs().sum())

    def _turnover(sel):
        target = {}
        if sel:
            w = timing_scale / len(sel)
            for code in sel:
                target[code] = w
        keys = set(prev_w.index) | set(target)
        s = 0.0
        for k in keys:
            s += abs(float(target.get(k, 0.0)) - float(prev_w.get(k, 0.0)))
        return 0.5 * s

    apply_cap = prev_gross > 1e-6
    for c in new_pool:
        if len(selected) >= TOP_N:
            break
        trial = selected + [c]
        if (not apply_cap) or _turnover(trial) <= MAX_TURNOVER + 1e-9:
            selected.append(c)
    if len(selected) > TOP_N:
        selected = sorted(selected, key=lambda x: float(rank_row.get(x, 1e9)))[:TOP_N]
    if not selected:
        selected = list(new_pool[:TOP_N])
    return set(selected)


def _build_weights(
    sort_score: pd.DataFrame, confirm_ok: pd.DataFrame, close: pd.DataFrame
) -> tuple[pd.DataFrame, int]:
    """F0-identical portfolio construction from precomputed scores."""
    rank_df = sort_score.rank(axis=1, ascending=False, method="first")
    dates = close.index
    is_reb = _rebalance_mask(dates)
    n_reb = int(np.asarray(is_reb).sum())
    sector_close = close.mean(axis=1)
    sector_ma = sector_close.rolling(MA_PERIOD, min_periods=5).mean()
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
            sc = sector_close.iloc[i]
            sma = sector_ma.iloc[i]
            if pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma):
                timing_scale = 0.5
            else:
                timing_scale = 1.0
            selected = _select_with_turnover_cap(
                rank_row,
                conf_row,
                holdings,
                entry_px,
                peak_px,
                entry_rank,
                hold_days,
                close,
                i,
                prev_w,
                timing_scale,
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
                        entry_rank[c] = (
                            float(rank_row[c]) if pd.notna(rank_row[c]) else BAND
                        )
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
                if (
                    _exit_reason(
                        c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, False
                    )
                    is not None
                ):
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


def _fast_metrics(weights: pd.DataFrame, close: pd.DataFrame) -> dict:
    """Rank-only PnL: next-bar close returns + turnover costs; EQW benchmark."""
    rets = close.pct_change().fillna(0.0)
    # Signal at t close → hold through t+1 (same lag convention as most daily engines).
    w_lag = weights.shift(1).fillna(0.0)
    gross = (w_lag * rets).sum(axis=1)

    delta = weights.diff().abs().sum(axis=1).fillna(0.0)
    # Rough cost: one-way on |Δw|, plus stamp-ish surcharge on reductions.
    sell = weights.diff().clip(upper=0).abs().sum(axis=1).fillna(0.0)
    costs = delta * COST_ONE_WAY + sell * COST_SELL_EXTRA
    port = gross - costs

    equity = (1.0 + port).cumprod() * 1_000_000.0
    bench_ret = rets.mean(axis=1)
    # Synthetic trade list empty — ranking uses return/sharpe/excess primarily.
    m = calc_metrics(
        equity,
        trades=[],
        initial_cash=1_000_000.0,
        bars_per_year=252,
        bench_ret=bench_ret,
        positions=weights,
    )
    return m


def _combo_label(confirm_ids: tuple[str, ...]) -> str:
    if not confirm_ids:
        return "pure_164"
    return "+".join(aid.replace("gtja191_", "") for aid in confirm_ids)


def _score_combo(
    confirm_ids: tuple[str, ...],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    close: pd.DataFrame,
) -> dict:
    mom = scores[MOM_ID]
    ok = _confirm_ok(confirm_ids, scores, irs, close)
    weights, n_reb = _build_weights(mom, ok, close)
    m = _fast_metrics(weights, close)
    return {
        "label": _combo_label(confirm_ids),
        "confirm_ids": list(confirm_ids),
        "n_confirm": len(confirm_ids),
        "is_baseline": list(confirm_ids) == list(BASELINE_CONFIRM),
        "is_rebalance_days": n_reb,
        "total_return": float(m.get("total_return", 0.0)),
        "excess_return": float(m.get("excess_return", 0.0)),
        "sharpe": float(m.get("sharpe", 0.0)),
        "max_drawdown": float(m.get("max_drawdown", 0.0)),
        "benchmark_return": float(m.get("benchmark_return", 0.0)),
        "rebalance_turnover_mean": float(m.get("rebalance_turnover_mean", 0.0) or 0.0),
        "irs": {aid: float(irs.get(aid, 0.0)) for aid in confirm_ids},
    }


def _iter_combos(cands: list[str], max_pair: int, include_pure: bool):
    if include_pure:
        yield tuple()
    yield BASELINE_CONFIRM
    if max_pair >= 1:
        for a in cands:
            yield (a,)
    if max_pair >= 2:
        for a, b in itertools.combinations(cands, 2):
            # Skip exact baseline duplicate (already yielded).
            if tuple(sorted((a, b))) == tuple(sorted(BASELINE_CONFIRM)):
                continue
            # Keep original IR-desc order later; store lexical for stability.
            yield (a, b)


class _CachedSignalEngine:
    """Full-engine adapter: mom/confirm from precomputed smooth scores."""

    def __init__(self, confirm_ids: list[str], scores: dict[str, pd.DataFrame], irs: dict[str, float]):
        self.mom_id = MOM_ID
        self.confirm_ids = list(confirm_ids)
        self.alpha_ids = [self.mom_id] + self.confirm_ids
        self.scores = scores
        self.irs = irs
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        close = pd.DataFrame({c: data_map[c]["close"] for c in data_map})
        close = close.sort_index()
        mom = self.scores[self.mom_id].reindex(index=close.index, columns=close.columns)
        ok = _confirm_ok(tuple(self.confirm_ids), self.scores, self.irs, close)
        weights, n_reb = _build_weights(mom, ok, close)
        self.last_is_rebalance_days = n_reb
        return {c: weights[c].copy() for c in close.columns}


def _full_engine_run(
    confirm_ids: list[str],
    scores: dict[str, pd.DataFrame],
    irs: dict[str, float],
    data_map: dict[str, pd.DataFrame],
    run_dir: Path,
    mc_sims: int,
) -> dict:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    cfg = {
        "codes": sorted(data_map),
        "source": "tushare",
        "start_date": "2024-01-01",
        "end_date": "2026-07-24",
        "interval": "1D",
        "initial_cash": 1_000_000,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": [MOM_ID] + list(confirm_ids),
        "parameters": {
            "strategy": f"confirm_search_{_combo_label(tuple(confirm_ids))}",
            "panel": "金属铜 BK1615",
            "confirm_ids": list(confirm_ids),
        },
        "validation": {"monte_carlo": {"n_simulations": mc_sims}},
        "_run_card_effective_sources": ["local_artifacts"],
    }
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sig = _CachedSignalEngine(confirm_ids, scores, irs)
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
        "label": _combo_label(tuple(confirm_ids)),
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


def _write_reports(df: pd.DataFrame, verified: list[dict], baseline_fast: dict) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_ROOT / "confirm_search_rank.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    top = df.head(20)
    lines = [
        "# 金属铜 BK1615 — GTJA191 存活因子确认腿搜索",
        "",
        "**声明：研究/回测，非实盘建议。**",
        "",
        "## 设定",
        "",
        f"- 动量主排序固定：`{MOM_ID}`",
        f"- 候选确认：存活 gtja191 排除 `{MOM_ID}` 与禁止反转 `{sorted(BANNED_CONFIRM)}`",
        f"- 过程约束：与铜 F0 相同（周频 / C≥40% / N=6 / 换手帽20% / MA20 择时）",
        f"- IR 权重来自 CSI300 存活库（非铜池重估）",
        f"- 快筛组合数：{len(df)}；基线 F0 = `171+054`",
        "",
        "## 快筛 Top20（按 excess_return）",
        "",
        "| rank | confirm | n | excess | total | sharpe | max_dd | baseline? |",
        "|-----:|---------|--:|-------:|------:|-------:|-------:|:---------:|",
    ]
    for i, row in enumerate(top.itertuples(index=False), 1):
        lines.append(
            f"| {i} | `{row.label}` | {row.n_confirm} | {_pct(row.excess_return)} | "
            f"{_pct(row.total_return)} | {_num(row.sharpe)} | {_pct(row.max_drawdown)} | "
            f"{'Y' if row.is_baseline else ''} |"
        )

    base_rank = int(df.index[df["is_baseline"]].tolist()[0]) + 1 if df["is_baseline"].any() else -1
    lines += [
        "",
        f"基线 `171+054` 快筛名次：#{base_rank}；"
        f"excess={_pct(baseline_fast['excess_return'])}，sharpe={_num(baseline_fast['sharpe'])}",
        "",
        "## 完整引擎复核（含 MC）",
        "",
        "| label | excess | total | sharpe | max_dd | MC p_sharpe | Δexcess vs F0 |",
        "|-------|-------:|------:|-------:|-------:|------------:|--------------:|",
    ]
    base_full = next((v for v in verified if v["label"] == "171+054"), None)
    base_ex = float((base_full or {}).get("metrics", {}).get("excess_return", 0.0))
    for v in verified:
        m = v["metrics"]
        mc = (v.get("validation") or {}).get("monte_carlo") or {}
        d_ex = float(m["excess_return"]) - base_ex
        lines.append(
            f"| `{v['label']}` | {_pct(m['excess_return'])} | {_pct(m['total_return'])} | "
            f"{_num(m['sharpe'])} | {_pct(m['max_drawdown'])} | {_num(mc.get('p_value_sharpe'))} | "
            f"{_pct(d_ex)} |"
        )

    winner = verified[0] if verified else None
    if winner and base_full:
        # Prefer highest full-engine excess among verified (baseline included).
        winner = max(verified, key=lambda x: float(x["metrics"]["excess_return"]))
        wm = winner["metrics"]
        bm = base_full["metrics"]
        beat = float(wm["excess_return"]) > float(bm["excess_return"]) + 1e-9
        lines += [
            "",
            "## 结论",
            "",
            f"- 完整引擎最优确认腿：`{winner['label']}`"
            f"（{'优于' if beat else '未优于'} 基线 171+054）",
            f"- 最优 excess={_pct(wm['excess_return'])}，sharpe={_num(wm['sharpe'])}",
            f"- 基线 excess={_pct(bm['excess_return'])}，sharpe={_num(bm['sharpe'])}",
            "- 铜板块样本短、商品周期强，确认腿重搜有样本内过拟合风险；需样本外复核。",
            "",
            f"明细 CSV：`{csv_path}`",
        ]
    REPORT_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Frontend-style prop for the verified winner.
    if winner:
        ids = winner["confirm_ids"]
        id_rows = "\n".join(
            [f"| {MOM_ID} | 动量（主排序，必须） |"]
            + [f"| {aid} | 确认（重搜硬确认） |" for aid in ids]
        )
        copy_ids = ", ".join([MOM_ID] + ids)
        # Pull IR from survival for the prop text.
        irs_path = survival_path()
        alphas = json.loads(irs_path.read_text(encoding="utf-8")).get("alphas") or {}
        ir_weighted = ", ".join(
            f"{aid.replace('gtja191_', '')}:{float((alphas.get(aid) or {}).get('ir') or 0.0):.3f}"
            for aid in ids
        )
        body = f"""## 提示词

```
请用国泰君安 191（gtja191）做多因子选股并回测，严格按下面约束执行，不要自行扩大因子范围。

【背景与本版目标】
本版为金属铜板块（东财行业「铜」BK1615）上，在 GTJA191 存活因子中重搜确认腿后的最优复现：
F0' = 动量 164 + 硬确认 { '+'.join(a.replace('gtja191_','') for a in ids) }。
对照原基线 171/054：完整引擎 Δexcess={_pct(float(winner['metrics']['excess_return']) - base_ex)}。
禁止为追收益叠加 custom 否决或扩因子。

优化看 excess_return；excess<0 或 MC p>0.2 必须明文承认。

【股票池与区间】
- 标的池：A股金属铜相关板块（东财行业「铜」BK1615，排除北交所）
- 研究/回测参考区间：2024-01-01 至 2026-07-24（数据不足则用可得区间并说明）
- 日频算因子与权重（interval=1D）；每周第一个交易日调仓；非调仓日禁止按新排名换股，只允许风控退出
- config.json 无生效 rebalance_frequency；规则写在 code/signal_engine.py。禁止臆造该字段

【周频（硬性）】
  week_id = year.astype(str) + '-' + week.astype(str)
  is_rebalance = ~week_id.duplicated(keep='first')
禁止：groupby(week_id).transform('first') 再比较（伪周频）。
交付打印 is_rebalance.sum()；若 > 日历周数×1.5 视为失败。

【核心因子（本版 {1 + len(ids)} 个）】
| ID | 角色 |
{id_rows}

复制用：{copy_ids}
【禁止】gtja191_114 / 反转因子；禁止 custom_max_inv / 吸筹 / 质量确认进硬过滤或重排

【评分与过滤（硬性）】
1. 各因子截面分位 → 5 日平滑
2. 排序分 S = smooth(gtja191_164)（纯动量）
3. 确认分 C = IR 加权({ir_weighted})；缺谁跳谁并重归一
4. 新开仓过滤：C ≥ 当日截面 40% 分位；确认全缺日不过滤
5. 面板只建 OHLCV；不要为 114 拼 amount

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
- 因子 ID + 角色；未使用反转/吸筹/custom
- 最近调仓日持仓表；指标含 total/benchmark/excess/sharpe/max_dd/trade_count/rebalance_count/is_rebalance_days/avg_holding_days/rebalance_turnover_mean；附 MC p
- 必须对照原基线 171/054：给出 Δtotal / Δexcess / Δsharpe
- 3–5 条白话：相对基准、铜价周期/确认腿样本内过拟合风险
- 声明：研究/回测，非实盘建议

```
"""
        PROP_OUT.write_text(body, encoding="utf-8")
        print(f"wrote prop {PROP_OUT}")
    print(f"wrote report {REPORT_OUT}")
    print(f"wrote csv {csv_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pair", type=int, default=2, choices=(1, 2))
    ap.add_argument("--top-k", type=int, default=5, help="full-engine verify top-K by fast excess")
    ap.add_argument("--mc-sims", type=int, default=1000)
    ap.add_argument("--include-pure", action="store_true", help="include pure_164 control")
    ap.add_argument(
        "--pair-pool",
        type=int,
        default=0,
        help="if >0, only form pairs among top-N singles by fast excess (after singles pass)",
    )
    args = ap.parse_args()

    irs = _load_alive_irs()
    if MOM_ID not in irs:
        raise SystemExit(f"{MOM_ID} not alive in survival store")
    print(f"[survival] alive gtja191={len(irs)}")

    data_map = _load_ohlcv()
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
    print(f"[search] confirm candidates={len(cands)} max_pair={args.max_pair}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    t0 = time.time()

    # Two-stage optional: if pair_pool>0, score singles first, then pairs among top.
    if args.max_pair >= 2 and args.pair_pool > 0:
        print("[stage1] singles + baseline…")
        stage1 = []
        for combo in _iter_combos(cands, max_pair=1, include_pure=args.include_pure):
            stage1.append(_score_combo(combo, scores, irs, close))
        singles = [r for r in stage1 if r["n_confirm"] == 1]
        singles.sort(key=lambda r: r["excess_return"], reverse=True)
        pool = [r["confirm_ids"][0] for r in singles[: args.pair_pool]]
        print(f"[stage2] pairs among top-{len(pool)} singles…")
        seen = {tuple(r["confirm_ids"]) for r in stage1}
        rows.extend(stage1)
        for a, b in itertools.combinations(pool, 2):
            combo = (a, b)
            if combo in seen or tuple(reversed(combo)) in seen:
                continue
            if tuple(sorted(combo)) == tuple(sorted(BASELINE_CONFIRM)):
                continue
            rows.append(_score_combo(combo, scores, irs, close))
    else:
        # Full singles + pairs among all candidates (dedupe baseline).
        seen: set[tuple[str, ...]] = set()
        combos = list(_iter_combos(cands, args.max_pair, args.include_pure))
        print(f"[search] combo count≈{len(combos)}")
        for i, combo in enumerate(combos, 1):
            key = tuple(combo)
            if key in seen:
                continue
            seen.add(key)
            rows.append(_score_combo(combo, scores, irs, close))
            if i % 100 == 0 or i == len(combos):
                print(f"  scored {i}/{len(combos)} elapsed={time.time() - t0:.1f}s")

    df = pd.DataFrame(rows).sort_values(
        ["excess_return", "sharpe", "total_return"], ascending=False
    ).reset_index(drop=True)
    baseline_fast = df.loc[df["is_baseline"]].iloc[0].to_dict()
    print(
        f"[fast] done n={len(df)} in {time.time() - t0:.1f}s; "
        f"best={df.iloc[0]['label']} excess={df.iloc[0]['excess_return']:.4f}; "
        f"baseline_rank=#{int(df.index[df['is_baseline']][0]) + 1}"
    )

    # Verify: baseline + top-K distinct (always include best single & best pair if present).
    verify_labels = []
    verify_ids: list[list[str]] = []

    def _add(ids: list[str]):
        lab = _combo_label(tuple(ids))
        if lab in verify_labels:
            return
        verify_labels.append(lab)
        verify_ids.append(ids)

    _add(list(BASELINE_CONFIRM))
    for _, row in df.head(max(args.top_k, 1)).iterrows():
        _add(list(row["confirm_ids"]))
    # Ensure best single / best pair specifically covered.
    best_single = df.loc[df["n_confirm"] == 1]
    best_pair = df.loc[df["n_confirm"] == 2]
    if len(best_single):
        _add(list(best_single.iloc[0]["confirm_ids"]))
    if len(best_pair):
        _add(list(best_pair.iloc[0]["confirm_ids"]))

    verified: list[dict] = []
    print(f"[full-engine] verifying {len(verify_ids)} combos…")
    for ids in verify_ids:
        lab = _combo_label(tuple(ids))
        run_dir = OUT_ROOT / f"full_{lab}"
        print(f"  → {lab}")
        result = _full_engine_run(ids, scores, irs, data_map, run_dir, args.mc_sims)
        # Attach IRs for prop writing.
        result["irs"] = {aid: float(irs.get(aid, 0.0)) for aid in ids}
        verified.append(result)
        m = result["metrics"]
        print(
            f"    excess={m.get('excess_return')} sharpe={m.get('sharpe')} "
            f"mc_p={(result.get('validation') or {}).get('monte_carlo', {}).get('p_value_sharpe')}"
        )

    # Sort verified by full excess for report conclusion.
    verified.sort(key=lambda x: float(x["metrics"]["excess_return"]), reverse=True)
    _write_reports(df, verified, baseline_fast)

    best = verified[0]
    print(
        f"[done] full-engine best confirm={best['label']} "
        f"excess={best['metrics'].get('excess_return')} "
        f"sharpe={best['metrics'].get('sharpe')}"
    )


if __name__ == "__main__":
    main()
