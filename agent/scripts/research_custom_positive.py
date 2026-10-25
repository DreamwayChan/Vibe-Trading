"""Find custom-factor integrations that beat F0 on BK1184 (full engine).

Base process = F0 (164 sort + hard 171/054 @40% + weekly/turnover/MA20).
Custom is NEVER a replacement for F0 confirms. Modes tested:

  rerank_*   : among F0-eligible names, pick TopN by blend(164, custom)
  size_*     : same names as F0 TopN, tilt weights by custom score
  veto_*     : F0 flow + block NEW entries only if custom in bottom quintile
  soft_s_*   : blend custom into S *before* ranking (still keep F0 hard confirm)

Run from agent/:
    python scripts/research_custom_positive.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import (  # noqa: E402
    _AutoLoader,
    _create_market_engine,
    _load_module_from_file,
)

SRC = _AGENT_ROOT / "runs" / "20260725_094658_01_531d4f"
OUT = _AGENT_ROOT / "runs" / "_research_custom_positive"

ENGINE = r'''
"""Parameterized F0 + custom integration engine ({variant})."""
import numpy as np
import pandas as pd

class SignalEngine:
    _IR = {{
        "gtja191_164": 0.145,
        "gtja191_171": 0.155,
        "gtja191_054": 0.152,
    }}

    def __init__(self):
        self.mom_id = "gtja191_164"
        self.confirm_ids = ["gtja191_171", "gtja191_054"]
        self.custom_ids = {custom_ids!r}
        self.mode = {mode!r}          # rerank | size | veto | soft_s | baseline
        self.blend_w = float({blend_w!r})  # weight on custom in blend
        self.veto_q = float({veto_q!r})    # block if custom < this CS quantile
        self.alpha_ids = [self.mom_id] + self.confirm_ids + list(self.custom_ids)
        self.norm_weights = {{aid: 0.0 for aid in self.alpha_ids}}
        self.norm_weights[self.mom_id] = 1.0
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
        self._custom_score = None  # filled in generate

    def generate(self, data_map):
        panel = self._build_panel(data_map)
        if panel is None:
            return self._zero_weights(data_map)
        close = panel["close"]
        pack = self._compute_scores(panel, close)
        if pack is None:
            return self._zero_weights(data_map)
        sort_score, confirm_ok, custom_score = pack
        self._custom_score = custom_score
        rank_df = sort_score.rank(axis=1, ascending=False, method="first")
        weights_df = self._build_weights(rank_df, confirm_ok, close, sort_score)
        return {{c: weights_df[c].copy() for c in close.columns}}

    def _build_panel(self, data_map):
        cf, of_, hf, lf, vf = {{}}, {{}}, {{}}, {{}}, {{}}
        for code, df in data_map.items():
            if df is None or df.empty:
                continue
            cf[code] = df["close"]; of_[code] = df["open"]
            hf[code] = df["high"]; lf[code] = df["low"]; vf[code] = df["volume"]
        if not cf:
            return None
        return {{
            "close": pd.DataFrame(cf), "open": pd.DataFrame(of_),
            "high": pd.DataFrame(hf), "low": pd.DataFrame(lf),
            "volume": pd.DataFrame(vf),
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

        # F0 confirms
        conf_num = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        conf_den = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        any_conf = False
        for aid in self.confirm_ids:
            sm = _one(aid)
            if sm is None:
                continue
            w = max(self._IR.get(aid, 0.0), 0.0)
            valid = sm.notna()
            conf_num = conf_num.add(sm.fillna(0.0) * w, fill_value=0.0)
            conf_den = conf_den.add(valid.astype(float) * w, fill_value=0.0)
            any_conf = True
        if any_conf:
            conf = conf_num.divide(conf_den.replace(0.0, np.nan))
            conf_thr = conf.quantile(self.confirm_q, axis=1)
            confirm_ok = conf.ge(conf_thr, axis=0).fillna(False)
            confirm_ok.loc[conf.isna().all(axis=1), :] = True
        else:
            confirm_ok = pd.DataFrame(True, index=close.index, columns=close.columns)

        # Custom aggregate (equal-weight among available)
        custom_score = None
        if self.custom_ids:
            cnum = pd.DataFrame(0.0, index=close.index, columns=close.columns)
            cden = pd.DataFrame(0.0, index=close.index, columns=close.columns)
            for aid in self.custom_ids:
                sm = _one(aid)
                if sm is None:
                    continue
                valid = sm.notna()
                cnum = cnum.add(sm.fillna(0.0), fill_value=0.0)
                cden = cden.add(valid.astype(float), fill_value=0.0)
            if float(cden.to_numpy().sum()) > 0:
                custom_score = cnum.divide(cden.replace(0.0, np.nan))

        sort_score = mom
        if self.mode == "soft_s" and custom_score is not None:
            w = self.blend_w
            sort_score = (1.0 - w) * mom + w * custom_score

        # Veto: tighten confirm_ok for names with very weak custom
        if self.mode == "veto" and custom_score is not None:
            thr = custom_score.quantile(self.veto_q, axis=1)
            veto_ok = custom_score.ge(thr, axis=0).fillna(True)
            # Only AND for new entries later; keep as extra mask column via confirm_ok
            confirm_ok = confirm_ok & veto_ok

        return sort_score, confirm_ok, custom_score

    def _pick_score_row(self, sort_row, custom_row):
        """Score used to choose among eligible names on a rebalance day."""
        if self.mode == "rerank" and custom_row is not None:
            w = self.blend_w
            # blend_w=1 → pure custom among eligible; 0 → pure sort
            return (1.0 - w) * sort_row + w * custom_row
        return sort_row

    def _build_weights(self, rank_df, confirm_ok, close, sort_score):
        dates = close.index
        is_reb = self._rebalance_mask(dates)
        self.last_is_rebalance_days = int(np.asarray(is_reb).sum())
        sector_close = close.mean(axis=1)
        sector_ma = sector_close.rolling(self.ma_period, min_periods=5).mean()
        weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
        holdings = set()
        entry_px, peak_px, entry_rank, hold_days = {{}}, {{}}, {{}}, {{}}
        prev_w = pd.Series(0.0, index=weights.columns)
        timing_scale = 1.0
        custom = self._custom_score

        for i in range(len(dates)):
            rank_row = rank_df.iloc[i]
            conf_row = confirm_ok.iloc[i]
            sort_row = sort_score.iloc[i]
            custom_row = custom.iloc[i] if custom is not None else None
            current = pd.Series(0.0, index=weights.columns)

            if is_reb[i]:
                sc = sector_close.iloc[i]; sma = sector_ma.iloc[i]
                timing_scale = 0.5 if (pd.notna(sc) and pd.notna(sma) and float(sc) < float(sma)) else 1.0
                selected = self._select_with_turnover_cap(
                    rank_row, conf_row, holdings, entry_px, peak_px, entry_rank, hold_days,
                    close, i, prev_w, timing_scale, sort_row, custom_row,
                )
                n_sel = len(selected)
                if n_sel > 0:
                    if self.mode == "size" and custom_row is not None:
                        raw = []
                        for c in selected:
                            v = custom_row.get(c, np.nan)
                            raw.append(0.5 if pd.isna(v) else float(v))
                        arr = np.asarray(raw, dtype=float)
                        # Softmax tilt; floor so no name is zeroed
                        arr = arr - np.nanmax(arr)
                        ex = np.exp(arr)
                        ex = np.where(np.isfinite(ex), ex, 0.0)
                        if ex.sum() <= 0:
                            wv = {{c: timing_scale / n_sel for c in selected}}
                        else:
                            # Mix equal-weight with tilt to avoid extreme concentration
                            tilt = ex / ex.sum()
                            eq = np.full(n_sel, 1.0 / n_sel)
                            mix = (1.0 - self.blend_w) * eq + self.blend_w * tilt
                            mix = mix / mix.sum()
                            wv = {{c: timing_scale * float(mix[j]) for j, c in enumerate(selected)}}
                    else:
                        wv = {{c: timing_scale / n_sel for c in selected}}

                    for c in selected:
                        current[c] = wv[c]
                        px = float(close.iloc[i][c])
                        if c not in holdings:
                            entry_px[c] = px; peak_px[c] = px
                            entry_rank[c] = float(rank_row[c]) if pd.notna(rank_row[c]) else self.band
                            hold_days[c] = 0
                        else:
                            peak_px[c] = max(peak_px.get(c, px), px)
                            hold_days[c] = hold_days.get(c, 0) + 1
                for c in holdings - selected:
                    entry_px.pop(c, None); peak_px.pop(c, None)
                    entry_rank.pop(c, None); hold_days.pop(c, None)
                holdings = selected; prev_w = current
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
                    if self._exit_reason(
                        c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, False
                    ) is not None:
                        continue
                    survivors.add(c)
                    current[c] = float(prev_w[c])
                for c in holdings - survivors:
                    entry_px.pop(c, None); peak_px.pop(c, None)
                    entry_rank.pop(c, None); hold_days.pop(c, None)
                holdings = survivors; prev_w = current
            weights.iloc[i] = current
        return weights

    def _select_with_turnover_cap(
        self, rank_row, conf_row, holdings, entry_px, peak_px, entry_rank, hold_days,
        close, i, prev_w, timing_scale, sort_row, custom_row,
    ):
        must_exit = set(); keep_candidates = []
        for c in sorted(holdings):
            px = float(close.iloc[i][c])
            if pd.isna(px):
                must_exit.add(c); continue
            if self._exit_reason(
                c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, True
            ) is not None:
                must_exit.add(c); continue
            keep_candidates.append(c)

        # Eligible universe: pass confirm and within momentum band
        pick_score = self._pick_score_row(sort_row, custom_row)
        ranked_mom = rank_row.dropna().sort_values()
        new_pool = []
        for c in ranked_mom.index:
            if c in must_exit or c in keep_candidates:
                continue
            try:
                passed = bool(conf_row[c])
            except Exception:
                passed = False
            if not passed:
                continue
            if float(ranked_mom[c]) <= float(self.band):
                new_pool.append(c)

        # For rerank: order new_pool by pick_score desc (higher better)
        if self.mode == "rerank" and pick_score is not None:
            new_pool = sorted(
                new_pool,
                key=lambda x: float(pick_score.get(x, -1e9)) if pd.notna(pick_score.get(x, np.nan)) else -1e9,
                reverse=True,
            )

        selected = list(keep_candidates)
        prev_gross = float(prev_w.abs().sum())

        def _turnover(sel):
            target = {{}}
            if sel:
                # turnover check uses equal weights proxy (size tilt ignored for cap)
                w = timing_scale / len(sel)
                for code in sel:
                    target[code] = w
            keys = set(prev_w.index) | set(target)
            return 0.5 * sum(abs(float(target.get(k, 0.0)) - float(prev_w.get(k, 0.0))) for k in keys)

        apply_cap = prev_gross > 1e-6
        for c in new_pool:
            if len(selected) >= self.top_n:
                break
            trial = selected + [c]
            if (not apply_cap) or _turnover(trial) <= self.max_turnover + 1e-9:
                selected.append(c)

        if len(selected) > self.top_n:
            # Trim by pick_score (rerank) or mom rank
            if self.mode == "rerank" and pick_score is not None:
                selected = sorted(
                    selected,
                    key=lambda x: float(pick_score.get(x, -1e9)) if pd.notna(pick_score.get(x, np.nan)) else -1e9,
                    reverse=True,
                )[: self.top_n]
            else:
                selected = sorted(selected, key=lambda x: float(rank_row.get(x, 1e9)))[: self.top_n]

        if not selected:
            selected = list(new_pool[: self.top_n])
        return set(selected)

    def _exit_reason(self, code, px, rank_row, entry_px, peak_px, entry_rank, hold_days, check_band):
        cost = entry_px.get(code); peak = peak_px.get(code, px)
        if cost is None or cost <= 0:
            return None
        days = hold_days.get(code, 0)
        if peak > 0 and (peak - px) / peak >= self.peak_dd_sl:
            return "peak_dd"
        if px / cost - 1.0 >= self.tp:
            return "take_profit"
        if days >= self.time_stop_days:
            er = entry_rank.get(code); cr = rank_row.get(code, np.nan)
            if er is not None and pd.notna(cr) and float(cr) - float(er) >= self.rank_worsen:
                return "time_stop"
        if check_band and days >= self.min_hold_days:
            cr = rank_row.get(code, np.nan)
            if pd.isna(cr) or float(cr) > self.band:
                return "band_exit"
        return None

    def _rebalance_mask(self, dates):
        d = pd.DatetimeIndex(dates)
        iso = d.isocalendar()
        week_id = pd.Series(iso["year"].astype(str) + "-" + iso["week"].astype(str), index=d)
        return (~week_id.duplicated(keep="first")).to_numpy()
'''


VARIANTS = {
    # Baseline
    "F0": {
        "custom_ids": [],
        "mode": "baseline",
        "blend_w": 0.0,
        "veto_q": 0.0,
    },
    # Rerank among F0-eligible: mild custom blend
    "rerank_to_30": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "rerank",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    "rerank_to_50": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "rerank",
        "blend_w": 0.50,
        "veto_q": 0.0,
    },
    "rerank_to_100": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "rerank",
        "blend_w": 1.00,
        "veto_q": 0.0,
    },
    "rerank_ivol_20": {
        "custom_ids": ["custom_ivol_inv"],
        "mode": "rerank",
        "blend_w": 0.20,
        "veto_q": 0.0,
    },
    "rerank_max_20": {
        "custom_ids": ["custom_max_inv"],
        "mode": "rerank",
        "blend_w": 0.20,
        "veto_q": 0.0,
    },
    "rerank_to_ivol_25": {
        "custom_ids": ["custom_abn_to_inv", "custom_ivol_inv"],
        "mode": "rerank",
        "blend_w": 0.25,
        "veto_q": 0.0,
    },
    "rerank_vp_30": {
        # vp helped as Top6 filter in diagnosis (negative block-pass)
        "custom_ids": ["custom_vp_corr"],
        "mode": "rerank",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    # Size tilt on F0 names
    "size_to_30": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "size",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    "size_to_50": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "size",
        "blend_w": 0.50,
        "veto_q": 0.0,
    },
    "size_ivol_30": {
        "custom_ids": ["custom_ivol_inv"],
        "mode": "size",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    "size_max_30": {
        "custom_ids": ["custom_max_inv"],
        "mode": "size",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    # Soft veto: only kill bottom-quintile custom (not top-heavy quality filter)
    "veto_to_20": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "veto",
        "blend_w": 0.0,
        "veto_q": 0.20,
    },
    "veto_ivol_20": {
        "custom_ids": ["custom_ivol_inv"],
        "mode": "veto",
        "blend_w": 0.0,
        "veto_q": 0.20,
    },
    "veto_max_20": {
        "custom_ids": ["custom_max_inv"],
        "mode": "veto",
        "blend_w": 0.0,
        "veto_q": 0.20,
    },
    # Soft into S while keeping F0 hard confirm
    "soft_s_to_20": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "soft_s",
        "blend_w": 0.20,
        "veto_q": 0.0,
    },
    "soft_s_to_30": {
        "custom_ids": ["custom_abn_to_inv"],
        "mode": "soft_s",
        "blend_w": 0.30,
        "veto_q": 0.0,
    },
    "soft_s_ivol_15": {
        "custom_ids": ["custom_ivol_inv"],
        "mode": "soft_s",
        "blend_w": 0.15,
        "veto_q": 0.0,
    },
}


def load_ohlcv(codes: list[str]) -> dict[str, pd.DataFrame]:
    art = SRC / "artifacts"
    data_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        path = art / f"ohlcv_{code}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path)
        date_col = "trade_date" if "trade_date" in df.columns else "date"
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col).sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        data_map[code] = df[keep].copy()
    return data_map


def run_one(name: str, spec: dict, base_config: dict, data_map: dict) -> dict:
    run_dir = OUT / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)
    src = ENGINE.format(
        variant=name,
        custom_ids=spec["custom_ids"],
        mode=spec["mode"],
        blend_w=spec["blend_w"],
        veto_q=spec["veto_q"],
    )
    (run_dir / "code" / "signal_engine.py").write_text(src, encoding="utf-8")
    cfg = dict(base_config)
    cfg["codes"] = list(data_map.keys())
    cfg["source"] = "tushare"
    cfg["_run_card_effective_sources"] = ["local_artifacts"]
    cfg.pop("validation", None)  # speed
    if "initial_cash" not in cfg and "initial_capital" in cfg:
        cfg["initial_cash"] = cfg["initial_capital"]
    (run_dir / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"pos_{name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    sig = mod.SignalEngine()
    loader = _AutoLoader(data_map)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year(cfg.get("interval", "1D"), "tushare")
    print(f"[+] {name} mode={spec['mode']} custom={spec['custom_ids']} w={spec['blend_w']}")
    engine.run_backtest(cfg, loader, sig, run_dir, bars_per_year=bpy)
    m = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    m["variant"] = name
    m["mode"] = spec["mode"]
    m["custom"] = "+".join(spec["custom_ids"]) if spec["custom_ids"] else "(none)"
    m["blend_w"] = spec["blend_w"]
    return m


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads((SRC / "config.json").read_text(encoding="utf-8"))
    data_map = load_ohlcv(list(config.get("codes") or []))
    print(f"loaded {len(data_map)} symbols")

    rows = [run_one(name, spec, config, data_map) for name, spec in VARIANTS.items()]
    df = pd.DataFrame(rows)
    base = df.loc[df["variant"] == "F0"].iloc[0]
    df["d_total"] = df["total_return"] - float(base["total_return"])
    df["d_excess"] = df["excess_return"] - float(base["excess_return"])
    df["d_sharpe"] = df["sharpe"] - float(base["sharpe"])
    df["beats_f0"] = (
        (df["d_excess"] > 1e-6)
        & (df["d_total"] > -1e-6)
        & (df["sharpe"] >= float(base["sharpe"]) - 0.05)
    )
    df = df.sort_values(["beats_f0", "d_excess", "d_sharpe"], ascending=[False, False, False])
    cols = [
        "variant", "mode", "custom", "blend_w",
        "total_return", "excess_return", "sharpe", "max_drawdown",
        "win_rate", "trade_count", "rebalance_turnover_mean",
        "d_total", "d_excess", "d_sharpe", "beats_f0",
    ]
    df = df[cols]
    df.to_csv(OUT / "positive_bakeoff.csv", index=False)

    winners = df[df["beats_f0"]]
    md = ["# Custom × F0 positive bake-off", ""]
    md.append(f"F0 baseline: total={base['total_return']:.4f}, excess={base['excess_return']:.4f}, sharpe={base['sharpe']:.4f}")
    md.append("")
    md.append(df.to_markdown(index=False))
    md.append("")
    if len(winners):
        md.append("## Winners (beat F0 on excess, not worse on total, sharpe within -0.05)")
        md.append("")
        md.append(winners.to_markdown(index=False))
    else:
        md.append("## Winners")
        md.append("")
        md.append("No variant strictly beat F0 under the gate. See nearest by d_excess below.")
        md.append("")
        md.append(df.head(5).to_markdown(index=False))
    (OUT / "positive_bakeoff.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n" + df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    print(f"\nwinners={len(winners)}  wrote {OUT/'positive_bakeoff.csv'}")


if __name__ == "__main__":
    main()
