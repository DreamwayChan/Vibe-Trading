"""Full-engine offline bake-off of post-E candidates on BK1184 cached OHLCV.

Variants (process constraints identical to C/E):
  C_replay   — original C confirms 114/171/054, panel includes amount
  pure_164   — no confirm filter
  soft_C     — S = 0.5*164 + 0.5*IR(114,171,054); no hard filter
  soft_054   — S = 0.5*164 + 0.5*054; no hard filter
  F_to_soft  — S = 0.7*164 + 0.3*abn_to_inv; no hard filter
  F_c_drop114— C hard q40 but confirm = 171+054 only (matches historical no-amount)

Writes metrics table under runs/_research_better_than_e/.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine, _load_module_from_file  # noqa: E402

SRC_RUN = _AGENT_ROOT / "runs" / "20260725_094658_01_531d4f"
OUT_ROOT = _AGENT_ROOT / "runs" / "_research_better_than_e"
_OHLCV_RE = re.compile(r"^ohlcv_(.+)\.csv$", re.IGNORECASE)

ENGINE_TEMPLATE = r'''
"""Auto-generated bake-off engine: {variant}."""
import numpy as np
import pandas as pd

class SignalEngine:
    _IR = {{
        "gtja191_164": 0.145,
        "gtja191_114": 0.176,
        "gtja191_171": 0.155,
        "gtja191_054": 0.152,
        "custom_abn_to_inv": 0.30,
    }}

    def __init__(self):
        self.mom_id = "gtja191_164"
        self.confirm_ids = {confirm_ids!r}
        self.mode = {mode!r}  # hard | soft | none
        self.soft_w = {soft_w!r}
        self.alpha_ids = [self.mom_id] + list(self.confirm_ids)
        self.norm_weights = {{self.mom_id: 1.0}}
        for a in self.confirm_ids:
            self.norm_weights[a] = 0.0
        self.smooth_w = 5
        self.top_n = 6
        self.band = 9
        self.max_turnover = 0.20
        self.min_hold_days = 5
        self.confirm_q = 0.40
        self.peak_dd_sl = 0.15
        self.tp = 0.35
        self.time_stop_days = 20
        self.rank_worsen = 10
        self.ma_period = 20
        self.last_is_rebalance_days = 0

    def generate(self, data_map):
        panel = self._build_panel(data_map)
        if panel is None:
            return self._zero_weights(data_map)
        close = panel["close"]
        pack = self._compute_scores(panel, close)
        if pack is None:
            return self._zero_weights(data_map)
        sort_score, confirm_ok = pack
        rank_df = sort_score.rank(axis=1, ascending=False, method="first")
        weights_df = self._build_weights(rank_df, confirm_ok, close)
        return {{c: weights_df[c].copy() for c in close.columns}}

    def _build_panel(self, data_map):
        cf, of_, hf, lf, vf, af = {{}}, {{}}, {{}}, {{}}, {{}}, {{}}
        for code, df in data_map.items():
            if df is None or df.empty:
                continue
            cf[code] = df["close"]; of_[code] = df["open"]
            hf[code] = df["high"]; lf[code] = df["low"]; vf[code] = df["volume"]
            if "amount" in df.columns:
                af[code] = df["amount"]
            else:
                af[code] = df["close"] * df["volume"]
        if not cf:
            return None
        return {{
            "close": pd.DataFrame(cf), "open": pd.DataFrame(of_),
            "high": pd.DataFrame(hf), "low": pd.DataFrame(lf),
            "volume": pd.DataFrame(vf), "amount": pd.DataFrame(af),
        }}

    def _zero_weights(self, data_map):
        out = {{}}
        for code, df in data_map.items():
            idx = df.index if df is not None and not df.empty else pd.DatetimeIndex([])
            out[code] = pd.Series(0.0, index=idx)
        return out

    def _smooth_cs(self, raw):
        cs = raw.rank(axis=1, pct=True, ascending=True)
        return cs.rolling(window=self.smooth_w, min_periods=3).mean()

    def _compute_scores(self, panel, close):
        try:
            from src.factors.registry import Registry
            registry = Registry()
        except Exception:
            return None

        def _one(aid):
            try:
                raw = registry.compute(aid, panel)
            except Exception:
                return None
            if raw is None or raw.empty:
                return None
            return self._smooth_cs(raw)

        mom = _one(self.mom_id)
        if mom is None:
            return None

        conf_num = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        conf_den = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        any_conf = False
        for aid in self.confirm_ids:
            sm = _one(aid)
            if sm is None:
                continue
            w = max(self._IR.get(aid, 1.0), 0.0)
            valid = sm.notna()
            conf_num = conf_num.add(sm.fillna(0.0) * w, fill_value=0.0)
            conf_den = conf_den.add(valid.astype(float) * w, fill_value=0.0)
            any_conf = True

        if any_conf:
            conf = conf_num.divide(conf_den.replace(0.0, np.nan))
        else:
            conf = None

        if self.mode == "soft" and conf is not None:
            w = float(self.soft_w)
            sort_score = (1.0 - w) * mom + w * conf
            confirm_ok = pd.DataFrame(True, index=close.index, columns=close.columns)
            return sort_score, confirm_ok

        if self.mode == "none" or conf is None:
            confirm_ok = pd.DataFrame(True, index=close.index, columns=close.columns)
            return mom, confirm_ok

        # hard confirm
        conf_thr = conf.quantile(self.confirm_q, axis=1)
        confirm_ok = conf.ge(conf_thr, axis=0).fillna(False)
        confirm_ok.loc[conf.isna().all(axis=1), :] = True
        return mom, confirm_ok

    def _build_weights(self, rank_df, confirm_ok, close):
        dates = close.index
        is_reb = self._rebalance_mask(dates)
        self.last_is_rebalance_days = int(np.asarray(is_reb).sum())
        sector_close = close.mean(axis=1)
        sector_ma = sector_close.rolling(self.ma_period, min_periods=5).mean()
        weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
        holdings = set(); entry_px = {{}}; peak_px = {{}}; entry_rank = {{}}; hold_days = {{}}
        prev_w = pd.Series(0.0, index=weights.columns); timing_scale = 1.0
        for i in range(len(dates)):
            rank_row = rank_df.iloc[i]; conf_row = confirm_ok.iloc[i]
            current = pd.Series(0.0, index=weights.columns)
            if is_reb[i]:
                sc = sector_close.iloc[i]; sma = sector_ma.iloc[i]
                timing_scale = 0.5 if (pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma)) else 1.0
                selected = self._select_with_turnover_cap(
                    rank_row, conf_row, holdings, entry_px, peak_px, entry_rank, hold_days,
                    close, i, prev_w, timing_scale,
                )
                n_sel = len(selected)
                if n_sel > 0:
                    wv = timing_scale / n_sel
                    for c in selected:
                        current[c] = wv
                        px = float(close.iloc[i][c])
                        if c not in holdings:
                            entry_px[c] = px; peak_px[c] = px
                            entry_rank[c] = float(rank_row[c]) if pd.notna(rank_row[c]) else self.band
                            hold_days[c] = 0
                        else:
                            peak_px[c] = max(peak_px.get(c, px), px)
                            hold_days[c] = hold_days.get(c, 0) + 1
                for c in holdings - selected:
                    entry_px.pop(c, None); peak_px.pop(c, None); entry_rank.pop(c, None); hold_days.pop(c, None)
                holdings = selected; prev_w = current
            else:
                survivors = set()
                for c in sorted(holdings):
                    if prev_w.get(c, 0.0) <= 0: continue
                    px = float(close.iloc[i][c])
                    if pd.isna(px): continue
                    peak_px[c] = max(peak_px.get(c, px), px)
                    hold_days[c] = hold_days.get(c, 0) + 1
                    if self._exit_reason(c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, False) is not None:
                        continue
                    survivors.add(c); current[c] = float(prev_w[c])
                for c in holdings - survivors:
                    entry_px.pop(c, None); peak_px.pop(c, None); entry_rank.pop(c, None); hold_days.pop(c, None)
                holdings = survivors; prev_w = current
            weights.iloc[i] = current
        return weights

    def _select_with_turnover_cap(self, rank_row, conf_row, holdings, entry_px, peak_px,
                                  entry_rank, hold_days, close, i, prev_w, timing_scale):
        must_exit = set(); keep_candidates = []
        for c in sorted(holdings):
            px = float(close.iloc[i][c])
            if pd.isna(px):
                must_exit.add(c); continue
            if self._exit_reason(c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, True) is not None:
                must_exit.add(c); continue
            keep_candidates.append(c)
        ranked = rank_row.dropna().sort_values(); new_pool = []
        for c in ranked.index:
            if c in must_exit or c in keep_candidates: continue
            try: passed = bool(conf_row[c])
            except Exception: passed = False
            if not passed: continue
            if float(ranked[c]) <= float(self.band): new_pool.append(c)
        selected = list(keep_candidates); prev_gross = float(prev_w.abs().sum())
        def _turnover(sel):
            target = {{}}
            if sel:
                w = timing_scale / len(sel)
                for code in sel: target[code] = w
            keys = set(prev_w.index) | set(target)
            return 0.5 * sum(abs(float(target.get(k, 0.0)) - float(prev_w.get(k, 0.0))) for k in keys)
        apply_cap = prev_gross > 1e-6
        for c in new_pool:
            if len(selected) >= self.top_n: break
            trial = selected + [c]
            if (not apply_cap) or _turnover(trial) <= self.max_turnover + 1e-9:
                selected.append(c)
        if len(selected) > self.top_n:
            selected = sorted(selected, key=lambda x: float(rank_row.get(x, 1e9)))[: self.top_n]
        if not selected:
            selected = list(new_pool[: self.top_n])
        return set(selected)

    def _exit_reason(self, code, px, rank_row, entry_px, peak_px, entry_rank, hold_days, check_band):
        cost = entry_px.get(code); peak = peak_px.get(code, px)
        if cost is None or cost <= 0: return None
        days = hold_days.get(code, 0)
        if peak > 0 and (peak - px) / peak >= self.peak_dd_sl: return "peak_dd"
        if px / cost - 1.0 >= self.tp: return "take_profit"
        if days >= self.time_stop_days:
            er = entry_rank.get(code); cr = rank_row.get(code, np.nan)
            if er is not None and pd.notna(cr) and float(cr) - float(er) >= self.rank_worsen:
                return "time_stop"
        if check_band and days >= self.min_hold_days:
            cr = rank_row.get(code, np.nan)
            if pd.isna(cr) or float(cr) > self.band: return "band_exit"
        return None

    def _rebalance_mask(self, dates):
        d = pd.DatetimeIndex(dates)
        iso = d.isocalendar()
        week_id = pd.Series(iso["year"].astype(str) + "-" + iso["week"].astype(str), index=d)
        return (~week_id.duplicated(keep="first")).to_numpy()
'''


VARIANTS = {
    "hard_054_only": {
        "confirm_ids": ["gtja191_054"],
        "mode": "hard",
        "soft_w": 0.0,
    },
    "hard_171_only": {
        "confirm_ids": ["gtja191_171"],
        "mode": "hard",
        "soft_w": 0.0,
    },
    "hard_vp_corr": {
        "confirm_ids": ["custom_vp_corr"],
        "mode": "hard",
        "soft_w": 0.0,
    },
    "hard_171_054_to": {
        # Keep proven C confirms; add mild turnover quality into hard C.
        "confirm_ids": ["gtja191_171", "gtja191_054", "custom_abn_to_inv"],
        "mode": "hard",
        "soft_w": 0.0,
    },
    "soft_164_054_hard171": {
        # Rank with soft 164/054, still require 171 hard gate — encoded as soft
        # on [054] only is insufficient; use soft_054-like first then separate.
        # Practical proxy: soft blend 164+054 (no hard). Kept for completeness.
        "confirm_ids": ["gtja191_054"],
        "mode": "soft",
        "soft_w": 0.40,
    },
}


def load_cached_ohlcv(run_dir: Path, codes: list[str]) -> dict[str, pd.DataFrame]:
    art = run_dir / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in df.columns]
        out = df[keep].copy()
        if "amount" not in out.columns:
            out["amount"] = out["close"] * out["volume"]
        data_map[code] = out
    return data_map


def run_one(name: str, spec: dict, base_config: dict, data_map: dict) -> dict:
    run_dir = OUT_ROOT / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)

    src = ENGINE_TEMPLATE.format(
        variant=name,
        confirm_ids=spec["confirm_ids"],
        mode=spec["mode"],
        soft_w=spec["soft_w"],
    )
    (run_dir / "code" / "signal_engine.py").write_text(src, encoding="utf-8")
    cfg = dict(base_config)
    cfg["codes"] = list(data_map.keys())
    cfg["source"] = "tushare"
    cfg["_run_card_effective_sources"] = ["local_artifacts"]
    # Skip MC for speed in bake-off.
    cfg.pop("validation", None)
    if "initial_cash" not in cfg and "initial_capital" in cfg:
        cfg["initial_cash"] = cfg["initial_capital"]
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"bake_{name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    engine_sig = mod.SignalEngine()
    loader = _AutoLoader(data_map)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year(cfg.get("interval", "1D"), "tushare")
    print(f"[bake] {name} ...")
    engine.run_backtest(cfg, loader, engine_sig, run_dir, bars_per_year=bpy)
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    metrics["variant"] = name
    metrics["is_rebalance_days"] = getattr(engine_sig, "last_is_rebalance_days", None)
    return metrics


def main() -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    config = json.loads((SRC_RUN / "config.json").read_text(encoding="utf-8"))
    codes = list(config.get("codes") or [])
    data_map = load_cached_ohlcv(SRC_RUN, codes)
    print(f"loaded {len(data_map)}/{len(codes)} symbols")

    rows = []
    for name, spec in VARIANTS.items():
        rows.append(run_one(name, spec, config, data_map))

    # Attach published C / E for reference
    for label, path in [
        ("C_published", SRC_RUN / "artifacts" / "metrics.csv"),
        (
            "E_published",
            _AGENT_ROOT
            / "runs"
            / "20260725_175000_01_quality_e"
            / "artifacts"
            / "metrics.csv",
        ),
    ]:
        m = pd.read_csv(path).iloc[0].to_dict()
        m["variant"] = label
        rows.append(m)

    df = pd.DataFrame(rows)
    cols = [
        "variant",
        "total_return",
        "excess_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "trade_count",
        "rebalance_count",
        "avg_holding_days",
        "rebalance_turnover_mean",
        "benchmark_return",
    ]
    df = df[[c for c in cols if c in df.columns]].sort_values(
        "excess_return", ascending=False
    )
    df.to_csv(OUT_ROOT / "bakeoff_metrics.csv", index=False)
    print("\n=== Bake-off (sorted by excess_return) ===")
    print(df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    (OUT_ROOT / "bakeoff_metrics.md").write_text(
        "# Bake-off metrics\n\n" + df.to_markdown(index=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
