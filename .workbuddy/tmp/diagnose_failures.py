"""Diagnose why a monitor run fails the promotion gate.

Two questions per run:

1. Is the edge broad or is it a handful of lucky trades? Recompute Sharpe after
   deleting the top 5% of winning trades, and measure how much of total PnL the
   best 10 trades carry.
2. Is the drawdown/vol profile the thing that sinks bootstrap, or is the mean
   return itself too thin? Compare realised Sharpe against the bootstrap lower
   bound, and look at the per-window spread.

Everything is computed from artifacts already in the run directory.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

RUNS = Path("E:/Vibe-Trading/agent/runs")


def sharpe(rets: np.ndarray, ann: int = 252) -> float:
    if len(rets) < 2:
        return 0.0
    std = rets.std(ddof=1)
    if std == 0 or not np.isfinite(std):
        return 0.0
    return float(rets.mean() / std * math.sqrt(ann))


def diagnose(run_dir: Path) -> dict:
    m = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    eq = pd.read_csv(run_dir / "artifacts" / "equity.csv", parse_dates=["timestamp"])
    tr = pd.read_csv(run_dir / "artifacts" / "trades.csv")
    val = json.loads((run_dir / "artifacts" / "validation.json").read_text(encoding="utf-8"))

    rets = eq["ret"].to_numpy()[1:]
    rets = rets[np.isfinite(rets)]

    exits = tr[tr["pnl"] != 0].reset_index(drop=True)
    pnls = exits["pnl"].to_numpy(dtype=float)
    total_pnl = pnls.sum()

    out: dict = {
        "run_id": run_dir.name,
        "total_return": round(float(m["total_return"]), 4),
        "benchmark_return": round(float(m["benchmark_return"]), 4),
        "excess_return": round(float(m["excess_return"]), 4),
        "annual_return": round(float(m["annual_return"]), 4),
        "max_dd": round(float(m["max_drawdown"]), 4),
        "sharpe": round(float(m["sharpe"]), 3),
        "info_ratio": round(float(m["information_ratio"]), 3),
        "win_rate": round(float(m["win_rate"]), 3),
        "pl_ratio": round(float(m["profit_loss_ratio"]), 3),
        "avg_hold_days": round(float(m["avg_holding_days"]), 1),
        "trades": int(m["trade_count"]),
        "rebalances": int(m["rebalance_count"]),
        "total_turnover": round(float(m["total_turnover"]), 2),
        "realised_sharpe": round(sharpe(rets), 3),
        "ann_vol": round(float(rets.std(ddof=1) * math.sqrt(252)), 4),
    }

    # Fragility: drop the best k% trades, recompute Sharpe on the daily curve is
    # not possible without per-trade marks, so rebuild an equity path from the
    # remaining PnLs in their original order instead.
    if len(pnls) >= 10:
        order = exits.sort_values("timestamp").index.to_numpy()
        chrono = exits.loc[order, "pnl"].to_numpy(dtype=float)
        k = max(1, int(round(len(chrono) * 0.05)))
        top_idx = set(np.argsort(chrono)[-k:].tolist())
        kept = np.array([p for i, p in enumerate(chrono) if i not in top_idx])
        cap = float(m.get("initial_cash", 1_000_000)) if "initial_cash" in m else 1_000_000.0

        def path_sharpe(seq: np.ndarray) -> float:
            equity = cap + np.cumsum(seq)
            r = np.diff(equity) / equity[:-1]
            return sharpe(r)

        out["sharpe_drop_top5pct"] = round(path_sharpe(kept), 3)
        out["sharpe_full_path"] = round(path_sharpe(chrono), 3)
        top10 = np.sort(pnls)[-10:]
        out["top10_pnl_share"] = round(float(top10.sum() / total_pnl), 3) if total_pnl else None
        winners = pnls[pnls > 0]
        out["winner_count"] = int(len(winners))
        out["avg_win"] = round(float(winners.mean()), 1) if len(winners) else 0.0
        out["avg_loss"] = round(float(pnls[pnls < 0].mean()), 1) if (pnls < 0).any() else 0.0

    # Per-window spread (same 5 windows the walk-forward check uses)
    wf = val.get("walk_forward") or {}
    wins = wf.get("windows") or []
    out["wf_returns"] = [w["return"] for w in wins]
    out["wf_consistency"] = wf.get("consistency_rate")

    bs = val.get("bootstrap") or {}
    out["ci_lower"] = bs.get("ci_lower")
    out["prob_positive"] = bs.get("prob_positive")
    mc = val.get("monte_carlo") or {}
    out["p_value"] = mc.get("p_value_sharpe")

    # Cost drag: gross vs net is not stored, so use turnover x (commission+slippage)
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    fee = float(cfg.get("commission", 0)) + float(cfg.get("slippage", 0))
    out["fee_per_side"] = round(fee, 5)
    out["cost_drag_est"] = round(float(m["total_turnover"]) * fee, 4)
    return out


rows = []
for d in sorted(p for p in RUNS.iterdir() if (p / "code" / "signal_engine.py").is_file()):
    try:
        rows.append(diagnose(d))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR {d.name}: {type(exc).__name__}: {exc}")

out_path = Path("E:/Vibe-Trading/.workbuddy/tmp/diagnostics.json")
out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

hdr = f"{'run':<50}{'ret':>8}{'exc':>8}{'shp':>7}{'vol':>7}{'dd':>8}{'s|cut':>8}{'top10':>7}{'p':>7}{'ci_lo':>7}"
print(hdr)
print("-" * len(hdr))
for r in rows:
    print(
        f"{r['run_id'][:49]:<50}{r['total_return']:>8.2f}{r['excess_return']:>8.2f}"
        f"{r['realised_sharpe']:>7.2f}{r['ann_vol']:>7.2f}{r['max_dd']:>8.2f}"
        f"{r.get('sharpe_drop_top5pct', float('nan')):>8.2f}{r.get('top10_pnl_share', float('nan')):>7.2f}"
        f"{r['p_value']:>7.3f}{(r['ci_lower'] if r['ci_lower'] is not None else float('nan')):>7.2f}"
    )
