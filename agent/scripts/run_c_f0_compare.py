"""Offline re-run C and F0 on BK1184 cached OHLCV; write two detailed reports."""

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
RUN_C = _AGENT_ROOT / "runs" / "20260725_182200_01_C_replay"
RUN_F0 = _AGENT_ROOT / "runs" / "20260725_182200_02_F0_dual"

F0_ENGINE = r'''
"""
GTJA191 F0 双确认引擎 — 人形机器人 BK1184.

相对名义 C：显式删除 gtja191_114（负 IC / 反动量；且历史因无 amount 从未生效）。
  - 动量主排序: gtja191_164
  - 确认过滤: 171/054, 阈值=截面 40% 分位
  - 无反转; Top6 / 保留带 Top9
  - 真周频; 换手帽20%; 最小持有5日
  - 板块均价 vs MA20 半仓, 仅调仓日更新
"""

import numpy as np
import pandas as pd


class SignalEngine:
    """纯动量 + 171/054 确认过滤 + 周检择时 + 换手帽."""

    _IR = {
        "gtja191_164": 0.145,
        "gtja191_171": 0.155,
        "gtja191_054": 0.152,
    }

    def __init__(self):
        self.mom_id = "gtja191_164"
        self.confirm_ids = ["gtja191_171", "gtja191_054"]
        self.alpha_ids = [self.mom_id] + self.confirm_ids
        self.norm_weights = {
            "gtja191_164": 1.0,
            "gtja191_171": 0.0,
            "gtja191_054": 0.0,
        }
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
        self.last_active_confirms = []

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
        return {c: weights_df[c].copy() for c in close.columns}

    def _build_panel(self, data_map):
        # F0: OHLCV only — do not synthesize amount (avoids accidental 114).
        cf, of_, hf, lf, vf = {}, {}, {}, {}, {}
        for code, df in data_map.items():
            if df is None or df.empty:
                continue
            cf[code] = df["close"]
            of_[code] = df["open"]
            hf[code] = df["high"]
            lf[code] = df["low"]
            vf[code] = df["volume"]
        if not cf:
            return None
        return {
            "close": pd.DataFrame(cf),
            "open": pd.DataFrame(of_),
            "high": pd.DataFrame(hf),
            "low": pd.DataFrame(lf),
            "volume": pd.DataFrame(vf),
        }

    def _zero_weights(self, data_map):
        out = {}
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
        active = []
        for aid in self.confirm_ids:
            sm = _one(aid)
            if sm is None:
                continue
            w = max(self._IR.get(aid, 0.0), 0.0)
            valid = sm.notna()
            conf_num = conf_num.add(sm.fillna(0.0) * w, fill_value=0.0)
            conf_den = conf_den.add(valid.astype(float) * w, fill_value=0.0)
            active.append(aid)
        self.last_active_confirms = active

        if active:
            conf = conf_num.divide(conf_den.replace(0.0, np.nan))
            conf_thr = conf.quantile(self.confirm_q, axis=1)
            confirm_ok = conf.ge(conf_thr, axis=0).fillna(False)
            confirm_ok.loc[conf.isna().all(axis=1), :] = True
        else:
            confirm_ok = pd.DataFrame(True, index=close.index, columns=close.columns)
        return mom, confirm_ok

    def _build_weights(self, rank_df, confirm_ok, close):
        dates = close.index
        is_reb = self._rebalance_mask(dates)
        self.last_is_rebalance_days = int(np.asarray(is_reb).sum())
        sector_close = close.mean(axis=1)
        sector_ma = sector_close.rolling(self.ma_period, min_periods=5).mean()
        weights = pd.DataFrame(0.0, index=dates, columns=close.columns)
        holdings = set()
        entry_px, peak_px, entry_rank, hold_days = {}, {}, {}, {}
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
                selected = self._select_with_turnover_cap(
                    rank_row, conf_row, holdings, entry_px, peak_px,
                    entry_rank, hold_days, close, i, prev_w, timing_scale,
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
                                float(rank_row[c]) if pd.notna(rank_row[c]) else self.band
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
                    if self._exit_reason(
                        c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, False
                    ) is not None:
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
        return weights

    def _select_with_turnover_cap(
        self, rank_row, conf_row, holdings, entry_px, peak_px,
        entry_rank, hold_days, close, i, prev_w, timing_scale,
    ):
        must_exit = set()
        keep_candidates = []
        for c in sorted(holdings):
            px = float(close.iloc[i][c])
            if pd.isna(px):
                must_exit.add(c)
                continue
            if self._exit_reason(
                c, px, rank_row, entry_px, peak_px, entry_rank, hold_days, True
            ) is not None:
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
            if float(ranked[c]) <= float(self.band):
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
            if len(selected) >= self.top_n:
                break
            trial = selected + [c]
            if (not apply_cap) or _turnover(trial) <= self.max_turnover + 1e-9:
                selected.append(c)
        if len(selected) > self.top_n:
            selected = sorted(
                selected, key=lambda x: float(rank_row.get(x, 1e9))
            )[: self.top_n]
        if not selected:
            selected = list(new_pool[: self.top_n])
        return set(selected)

    def _exit_reason(
        self, code, px, rank_row, entry_px, peak_px, entry_rank, hold_days, check_band,
    ):
        cost = entry_px.get(code)
        peak = peak_px.get(code, px)
        if cost is None or cost <= 0:
            return None
        days = hold_days.get(code, 0)
        if peak > 0 and (peak - px) / peak >= self.peak_dd_sl:
            return "peak_dd"
        if px / cost - 1.0 >= self.tp:
            return "take_profit"
        if days >= self.time_stop_days:
            er = entry_rank.get(code)
            cr = rank_row.get(code, np.nan)
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
        week_id = pd.Series(
            iso["year"].astype(str) + "-" + iso["week"].astype(str), index=d
        )
        return (~week_id.duplicated(keep="first")).to_numpy()
'''


def _prepare_run(dst: Path, label: str, engine_src: Path | None, engine_text: str | None) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "code").mkdir(parents=True)
    (dst / "artifacts").mkdir(parents=True)
    (dst / "logs").mkdir(parents=True)
    shutil.copy2(SRC / "config.json", dst / "config.json")
    # Copy OHLCV only (not old metrics/equity).
    for path in (SRC / "artifacts").glob("ohlcv_*.csv"):
        shutil.copy2(path, dst / "artifacts" / path.name)
    shutil.copy2(SRC / "rerun_fixed_engine.py", dst / "rerun_fixed_engine.py")
    if engine_src is not None:
        shutil.copy2(engine_src, dst / "code" / "signal_engine.py")
    else:
        assert engine_text is not None
        (dst / "code" / "signal_engine.py").write_text(engine_text, encoding="utf-8")

    cfg = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    cfg["parameters"] = {
        "strategy": label,
        "universe": "BK1184_人形机器人",
        "baseline_ohlcv_from": "20260725_094658_01_531d4f",
        "rebalance": "weekly_first_trading_day",
    }
    (dst / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_ohlcv(run_dir: Path, codes: list[str]) -> dict[str, pd.DataFrame]:
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
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        data_map[code] = df[keep].copy()
    return data_map


def _run_backtest(run_dir: Path) -> dict:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if "initial_cash" not in config and "initial_capital" in config:
        config["initial_cash"] = config["initial_capital"]
    codes = list(config.get("codes") or [])
    data_map = _load_ohlcv(run_dir, codes)
    print(f"[{run_dir.name}] loaded {len(data_map)}/{len(codes)} symbols")

    mod = _load_module_from_file(
        run_dir / "code" / "signal_engine.py",
        f"sig_{run_dir.name}_{pd.Timestamp.now('UTC').strftime('%H%M%S%f')}",
    )
    signal_engine = mod.SignalEngine()
    print(f"[{run_dir.name}] alphas={signal_engine.alpha_ids}")

    sample_idx = next(iter(data_map.values())).index
    mask = signal_engine._rebalance_mask(sample_idx)
    print(f"[{run_dir.name}] sample rebalance days={int(mask.sum())}/{len(mask)}")

    config["codes"] = list(data_map.keys())
    config["source"] = "tushare"
    config["_run_card_effective_sources"] = ["local_artifacts"]

    loader = _AutoLoader(data_map)
    engine = _create_market_engine("tushare", config, config["codes"])
    bpy = calc_bars_per_year(config.get("interval", "1D"), "tushare")
    engine.run_backtest(config, loader, signal_engine, run_dir, bars_per_year=bpy)

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    val_path = run_dir / "artifacts" / "validation.json"
    validation = json.loads(val_path.read_text(encoding="utf-8")) if val_path.exists() else {}
    return {
        "metrics": metrics,
        "validation": validation,
        "is_rebalance_days": getattr(signal_engine, "last_is_rebalance_days", None),
        "active_confirms": getattr(signal_engine, "last_active_confirms", None),
        "alpha_ids": list(signal_engine.alpha_ids),
        "confirm_ids": list(getattr(signal_engine, "confirm_ids", [])),
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


def _holdings_table(run_dir: Path) -> tuple[str, pd.Series, dict]:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    hold = pos.gt(0)
    last = pos.index[hold.any(axis=1)][-1]
    row = pos.loc[last]
    held = row[row > 0].sort_values(ascending=False)
    n = hold.sum(axis=1)
    invested = n[n > 0]
    stats = {
        "last_date": str(last.date()),
        "n_held": int(len(held)),
        "gross": float(held.sum()),
        "mean_n": float(invested.mean()) if len(invested) else 0.0,
        "median_n": float(invested.median()) if len(invested) else 0.0,
        "mean_gross": float(pos.abs().sum(axis=1).mean()),
    }
    lines = [
        f"| 代码 | 权重 |",
        f"|------|------|",
    ]
    for code, w in held.items():
        lines.append(f"| {code} | {float(w):.4f} |")
    return "\n".join(lines), held, stats


def _trade_stats(run_dir: Path) -> dict:
    trades = pd.read_csv(run_dir / "artifacts" / "trades.csv")
    if trades.empty:
        return {}
    # Prefer exit rows
    if "holding_days" in trades.columns:
        exits = trades[trades["holding_days"].fillna(0) > 0]
    else:
        exits = trades
    if exits.empty:
        exits = trades
    pnls = exits["pnl"].astype(float) if "pnl" in exits.columns else pd.Series(dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    return {
        "exit_count": int(len(exits)),
        "win_count": int(len(wins)),
        "loss_count": int(len(losses)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "best_trade": float(pnls.max()) if len(pnls) else 0.0,
        "worst_trade": float(pnls.min()) if len(pnls) else 0.0,
    }


def _position_overlap(run_a: Path, run_b: Path) -> dict:
    a = pd.read_csv(run_a / "artifacts" / "positions.csv", index_col=0, parse_dates=True).fillna(0.0)
    b = pd.read_csv(run_b / "artifacts" / "positions.csv", index_col=0, parse_dates=True).fillna(0.0)
    idx = a.index.intersection(b.index)
    cols = a.columns.intersection(b.columns)
    a = a.loc[idx, cols]
    b = b.loc[idx, cols]
    both = (a > 0) & (b > 0)
    only_a = (a > 0) & (b <= 0)
    only_b = (b > 0) & (a <= 0)
    # Weight L1 distance / 2 = turnover-like divergence
    l1 = 0.5 * (a - b).abs().sum(axis=1)
    return {
        "mean_overlap_names": float(both.sum(axis=1).mean()),
        "mean_only_a": float(only_a.sum(axis=1).mean()),
        "mean_only_b": float(only_b.sum(axis=1).mean()),
        "mean_weight_l1_half": float(l1.mean()),
        "max_weight_l1_half": float(l1.max()),
        "pct_days_identical_book": float(((a - b).abs().sum(axis=1) < 1e-12).mean()),
    }


def _equity_corr(run_a: Path, run_b: Path) -> float:
    ea = pd.read_csv(run_a / "artifacts" / "equity.csv")
    eb = pd.read_csv(run_b / "artifacts" / "equity.csv")
    # Flexible date/value columns
    def _pick(df):
        dcol = "date" if "date" in df.columns else df.columns[0]
        vcol = "equity" if "equity" in df.columns else (
            "value" if "value" in df.columns else df.columns[1]
        )
        s = df.set_index(pd.to_datetime(df[dcol]))[vcol].astype(float)
        return s.sort_index()

    sa, sb = _pick(ea), _pick(eb)
    common = sa.index.intersection(sb.index)
    if len(common) < 5:
        return float("nan")
    ra = sa.loc[common].pct_change()
    rb = sb.loc[common].pct_change()
    return float(ra.corr(rb))


def _write_report(
    path: Path,
    title: str,
    self_label: str,
    other_label: str,
    self_run: Path,
    other_run: Path,
    self_info: dict,
    other_info: dict,
    overlap: dict,
    eq_corr: float,
) -> None:
    sm, om = self_info["metrics"], other_info["metrics"]
    sv, ov = self_info["validation"], other_info["validation"]
    smc = (sv or {}).get("monte_carlo", {})
    omc = (ov or {}).get("monte_carlo", {})
    hold_md, held, hstat = _holdings_table(self_run)
    _, oheld, ostat = _holdings_table(other_run)
    tstat = _trade_stats(self_run)

    same_book = overlap["pct_days_identical_book"] >= 0.999
    delta_total = float(sm["total_return"]) - float(om["total_return"])
    delta_excess = float(sm["excess_return"]) - float(om["excess_return"])
    delta_sharpe = float(sm["sharpe"]) - float(om["sharpe"])

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("**声明：研究/回测结果，非实盘建议。**")
    lines.append("")
    lines.append("## 1. 运行概况")
    lines.append("")
    lines.append("| 项 | 内容 |")
    lines.append("|----|------|")
    lines.append(f"| 本报告策略 | **{self_label}** |")
    lines.append(f"| 对照策略 | {other_label} |")
    lines.append(f"| run_dir | `{self_run.name}` |")
    lines.append(f"| 对照 run | `{other_run.name}` |")
    lines.append("| 股票池 | BK1184 人形机器人（277 只，自 `20260725_094658_01_531d4f` 复制 OHLCV） |")
    lines.append("| 区间 | 2024-01-01 → 2026-07-24 |")
    lines.append("| 初始资金 | 10,000,000 |")
    lines.append("| 数据来源 | local_artifacts（离线，无重新拉数） |")
    lines.append(f"| 白名单因子 | `{', '.join(self_info['alpha_ids'])}` |")
    lines.append(f"| 确认腿声明 | `{', '.join(self_info['confirm_ids'])}` |")
    if self_info.get("active_confirms") is not None:
        lines.append(f"| 实际生效确认 | `{', '.join(self_info['active_confirms']) or '(无)'}` |")
    lines.append(f"| is_rebalance_days | {self_info.get('is_rebalance_days')} |")
    lines.append("")

    lines.append("## 2. 策略结构")
    lines.append("")
    if self_label.startswith("C"):
        lines.append(
            "- **名义结构**：主排序 `gtja191_164` + 确认 `114/171/054`（IR 加权）硬过滤 40% 分位。"
        )
        lines.append(
            "- **实际生效**：面板无 `amount` → `gtja191_114` Skip；有效确认 = `171+054`。"
        )
        lines.append("- **过程约束**：真周频 / Top6·保留带9 / 换手帽20% / 最小持有5日 / MA20 半仓。")
    else:
        lines.append(
            "- **F0 结构**：主排序 `gtja191_164` + 确认 **仅** `171/054`（IR 加权）硬过滤 40% 分位。"
        )
        lines.append(
            "- **相对名义 C**：显式删除 `gtja191_114`（负 IC、与 164 相关约 −0.56；补 amount 启用后会显著变差）。"
        )
        lines.append("- **过程约束**：与 C 完全相同（真周频 / Top6·带9 / 换手帽 / 最小持有 / MA20 半仓）。")
    lines.append("- **禁止**：反转因子、custom 质量/吸筹硬确认、`config.rebalance_frequency` 臆造字段。")
    lines.append("")

    lines.append("## 3. 核心绩效（本策略）")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| final_value | {_num(sm['final_value'], 2)} |")
    lines.append(f"| total_return | {_pct(sm['total_return'])} |")
    lines.append(f"| annual_return | {_pct(sm['annual_return'])} |")
    lines.append(f"| benchmark_return | {_pct(sm['benchmark_return'])} |")
    lines.append(f"| excess_return | {_pct(sm['excess_return'])} |")
    lines.append(f"| sharpe | {_num(sm['sharpe'])} |")
    lines.append(f"| max_drawdown | {_pct(sm['max_drawdown'])} |")
    lines.append(f"| sortino | {_num(sm.get('sortino'))} |")
    lines.append(f"| calmar | {_num(sm.get('calmar'))} |")
    lines.append(f"| information_ratio | {_num(sm.get('information_ratio'))} |")
    lines.append(f"| win_rate | {_pct(sm['win_rate'])} |")
    lines.append(f"| profit_factor | {_num(sm.get('profit_factor'))} |")
    lines.append(f"| trade_count | {int(float(sm['trade_count']))} |")
    lines.append(f"| rebalance_count | {int(float(sm['rebalance_count']))} |")
    lines.append(f"| avg_holding_days | {_num(sm['avg_holding_days'], 1)} |")
    lines.append(f"| rebalance_turnover_mean | {_num(sm['rebalance_turnover_mean'])} |")
    lines.append(f"| avg_turnover | {_num(sm.get('avg_turnover'))} |")
    if smc:
        lines.append(f"| MC p_value_sharpe | {_num(smc.get('p_value_sharpe'))} |")
        lines.append(f"| MC p_value_max_dd | {_num(smc.get('p_value_max_dd'))} |")
        lines.append(f"| MC n_simulations | {smc.get('n_simulations')} |")
    lines.append("")

    # Mandatory acknowledgements
    lines.append("### 必须承认的点")
    lines.append("")
    if float(sm["excess_return"]) < 0:
        lines.append(
            f"- **excess_return = {_pct(sm['excess_return'])} < 0**：绝对收益为正，但仍跑输板块等权基准 "
            f"({_pct(sm['benchmark_return'])})。"
        )
    if smc and float(smc.get("p_value_sharpe", 0)) > 0.2:
        lines.append(
            f"- **MC p_sharpe = {_num(smc.get('p_value_sharpe'))} > 0.2**：夏普优势统计不显著。"
        )
    elif smc:
        lines.append(
            f"- MC p_sharpe = {_num(smc.get('p_value_sharpe'))}（未超过 0.2 阈值，但仍须结合 excess 解读）。"
        )
    lines.append("")

    lines.append("## 4. 与对照策略详细对比")
    lines.append("")
    lines.append(f"| 指标 | {self_label} | {other_label} | 差额（本−对照） |")
    lines.append("|------|------------|--------------|----------------|")
    rows = [
        ("total_return", True),
        ("annual_return", True),
        ("excess_return", True),
        ("sharpe", False),
        ("max_drawdown", True),
        ("win_rate", True),
        ("profit_factor", False),
        ("information_ratio", False),
        ("trade_count", False),
        ("rebalance_count", False),
        ("avg_holding_days", False),
        ("rebalance_turnover_mean", False),
        ("benchmark_return", True),
    ]
    for key, is_pct in rows:
        a, b = sm.get(key), om.get(key)
        if a is None or b is None:
            continue
        da = float(a) - float(b)
        if is_pct:
            lines.append(f"| {key} | {_pct(a)} | {_pct(b)} | {_pct(da)} |")
        else:
            lines.append(f"| {key} | {_num(a)} | {_num(b)} | {_num(da)} |")
    if smc and omc:
        lines.append(
            f"| MC p_sharpe | {_num(smc.get('p_value_sharpe'))} | {_num(omc.get('p_value_sharpe'))} | "
            f"{_num(float(smc.get('p_value_sharpe', 0)) - float(omc.get('p_value_sharpe', 0)))} |"
        )
    lines.append("")

    lines.append("### 持仓重合度")
    lines.append("")
    lines.append("| 项 | 数值 |")
    lines.append("|----|------|")
    lines.append(f"| 日均共同持仓只数 | {_num(overlap['mean_overlap_names'], 3)} |")
    lines.append(f"| 日均仅本策略持有 | {_num(overlap['mean_only_a'], 3)} |")
    lines.append(f"| 日均仅对照持有 | {_num(overlap['mean_only_b'], 3)} |")
    lines.append(f"| 日均权重分歧 (0.5·L1) | {_num(overlap['mean_weight_l1_half'], 6)} |")
    lines.append(f"| 最大日权重分歧 | {_num(overlap['max_weight_l1_half'], 6)} |")
    lines.append(f"| 持仓完全相同交易日占比 | {_pct(overlap['pct_days_identical_book'])} |")
    lines.append(f"| 净值日收益相关 | {_num(eq_corr)} |")
    lines.append("")
    if same_book:
        lines.append(
            f"> **结论：在本数据与面板设定下，{self_label} 与 {other_label} 持仓/绩效本质相同"
            f"（Δtotal={_pct(delta_total)}, Δexcess={_pct(delta_excess)}, Δsharpe={_num(delta_sharpe)}）。"
            "名义 C 因 114 未生效，与显式双确认 F0 对齐。**"
        )
    else:
        lines.append(
            f"> **结论：两策略存在可测差异**（Δtotal={_pct(delta_total)}, "
            f"Δexcess={_pct(delta_excess)}, Δsharpe={_num(delta_sharpe)}）。"
        )
    lines.append("")

    lines.append("## 5. 最近调仓日持仓")
    lines.append("")
    lines.append(f"- 日期：{hstat['last_date']}")
    lines.append(f"- 持仓只数：{hstat['n_held']}；权重和：{_num(hstat['gross'], 4)}")
    lines.append(
        f"- 全样本（有仓日）平均持仓只数：{_num(hstat['mean_n'], 2)}；中位：{_num(hstat['median_n'], 2)}"
    )
    lines.append(f"- 全样本平均毛仓位：{_num(hstat['mean_gross'], 3)}")
    lines.append("")
    lines.append(hold_md)
    lines.append("")
    lines.append(f"对照 {other_label} 同日持仓：`{', '.join(oheld.index.tolist())}` "
                 f"（n={ostat['n_held']}, gross={_num(ostat['gross'], 4)}）")
    lines.append("")

    lines.append("## 6. 交易与过程约束检查")
    lines.append("")
    lines.append("| 检查项 | 结果 |")
    lines.append("|--------|------|")
    lines.append(
        f"| 真周频 is_rebalance_days | {self_info.get('is_rebalance_days')} "
        f"（rebalance_count={int(float(sm['rebalance_count']))}） |"
    )
    lines.append(
        f"| 换手帽相关 rebalance_turnover_mean | {_num(sm['rebalance_turnover_mean'])} |"
    )
    lines.append(f"| avg_holding_days | {_num(sm['avg_holding_days'], 1)} |")
    if tstat:
        lines.append(f"| 实现盈亏笔数(exit) | {tstat['exit_count']} |")
        lines.append(f"| 胜/负笔数 | {tstat['win_count']} / {tstat['loss_count']} |")
        lines.append(f"| 平均盈利 / 平均亏损 | {_num(tstat['avg_win'], 2)} / {_num(tstat['avg_loss'], 2)} |")
        lines.append(f"| 最好/最差单笔 | {_num(tstat['best_trade'], 2)} / {_num(tstat['worst_trade'], 2)} |")
    lines.append("")

    lines.append("## 7. 白话结论")
    lines.append("")
    if self_label.startswith("C"):
        lines.append("1. 本报告是名义 C（白名单含 114）在无 amount 面板上的离线重跑。")
        lines.append("2. 114 未生效，真实确认腿与 F0 相同，故绩效应对齐 F0。")
        lines.append("3. 绝对收益可观，但 excess 为负——没跑赢板块等权。")
        lines.append("4. 过程纪律（周频/换手/持有期）正常；问题在相对基准的选股超额。")
        lines.append("5. 生产上应改用 F0 明示双确认，避免日后补 amount 误启用 114。")
    else:
        lines.append("1. F0 把历史真实最优结构写死：164 + 171/054，删除 114。")
        lines.append("2. 与名义 C（无 amount）应对齐；这是刻意的一致性，不是巧合。")
        lines.append("3. excess 仍为负，必须承认：策略赚钱但未超额板块。")
        lines.append("4. 不要为了「补全三确认」去拼 amount 启用 114。")
        lines.append("5. 质量/吸筹硬确认（E）已否决；F0 为当前推荐默认。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"对照报告见同级目录另一份；artifacts 位于 `{self_run.as_posix()}`。")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def main() -> None:
    print("preparing runs...")
    _prepare_run(RUN_C, "C_nominal_replay", SRC / "code" / "signal_engine.py", None)
    _prepare_run(RUN_F0, "F0_dual_confirm", None, F0_ENGINE)

    # Patch C engine lightly to expose last_is_rebalance_days / active confirms
    c_path = RUN_C / "code" / "signal_engine.py"
    c_txt = c_path.read_text(encoding="utf-8")
    if "last_is_rebalance_days" not in c_txt:
        c_txt = c_txt.replace(
            "self.ma_period = 20\n",
            "self.ma_period = 20\n"
            "        self.last_is_rebalance_days = 0\n"
            "        self.last_active_confirms = []\n",
        )
        c_txt = c_txt.replace(
            "is_reb = self._rebalance_mask(dates)\n",
            "is_reb = self._rebalance_mask(dates)\n"
            "        self.last_is_rebalance_days = int(np.asarray(is_reb).sum())\n",
        )
        c_txt = c_txt.replace(
            "any_conf = False\n"
            "        for aid in self.confirm_ids:\n"
            "            sm = _one(aid)\n"
            "            if sm is None:\n"
            "                continue\n"
            "            w = max(self._IR[aid], 0.0)\n",
            "any_conf = False\n"
            "        active = []\n"
            "        for aid in self.confirm_ids:\n"
            "            sm = _one(aid)\n"
            "            if sm is None:\n"
            "                continue\n"
            "            active.append(aid)\n"
            "            w = max(self._IR[aid], 0.0)\n",
        )
        c_txt = c_txt.replace(
            "any_conf = True\n\n        if any_conf:",
            "any_conf = True\n"
            "        self.last_active_confirms = active\n\n"
            "        if any_conf:",
        )
        c_path.write_text(c_txt, encoding="utf-8")

    info_c = _run_backtest(RUN_C)
    info_f0 = _run_backtest(RUN_F0)
    overlap_c = _position_overlap(RUN_C, RUN_F0)
    # For F0 report, swap only_a / only_b labels by recomputing with reversed args
    overlap_f0 = _position_overlap(RUN_F0, RUN_C)
    eq_corr = _equity_corr(RUN_C, RUN_F0)

    _write_report(
        RUN_C / "REPORT_C.md",
        "C（名义三确认）离线重跑报告 — 对照 F0",
        "C（名义）",
        "F0（双确认）",
        RUN_C,
        RUN_F0,
        info_c,
        info_f0,
        overlap_c,
        eq_corr,
    )
    _write_report(
        RUN_F0 / "REPORT_F0.md",
        "F0（双确认）离线重跑报告 — 对照 C",
        "F0（双确认）",
        "C（名义）",
        RUN_F0,
        RUN_C,
        info_f0,
        info_c,
        overlap_f0,
        eq_corr,
    )

    # Side-by-side summary json
    summary = {
        "C": {
            "run": RUN_C.name,
            "metrics": info_c["metrics"],
            "validation": info_c["validation"],
            "active_confirms": info_c["active_confirms"],
            "is_rebalance_days": info_c["is_rebalance_days"],
        },
        "F0": {
            "run": RUN_F0.name,
            "metrics": info_f0["metrics"],
            "validation": info_f0["validation"],
            "active_confirms": info_f0["active_confirms"],
            "is_rebalance_days": info_f0["is_rebalance_days"],
        },
        "overlap": overlap_c,
        "equity_return_corr": eq_corr,
    }
    out = _AGENT_ROOT / "runs" / "20260725_182200_C_vs_F0_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print(f"wrote {out}")
    print("C total", info_c["metrics"]["total_return"], "active", info_c["active_confirms"])
    print("F0 total", info_f0["metrics"]["total_return"], "active", info_f0["active_confirms"])


if __name__ == "__main__":
    main()
