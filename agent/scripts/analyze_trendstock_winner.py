"""Robustness checks for the BK1715 趋势股 winner (split-sample + attribution).

Run from ``agent/``:
    python scripts/analyze_trendstock_winner.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from scripts.research_qlib158_trendstock_confirm_search import (  # noqa: E402
    _combo_label,
    _holdings_md,
    _name_map,
    _num,
    _pct,
)
from scripts.research_trendstock_stage2 import OUT_ROOT, _Engine, _run  # noqa: E402
from scripts.research_qlib158_trendstock_confirm_search import (  # noqa: E402
    SRC_RUN,
    _build_panel,
    _load_irs,
    _load_ohlcv_src,
    _pool_ic,
    _smooth_cs,
)
from src.factors.registry import Registry  # noqa: E402

WINNER_RUN = _AGENT_ROOT / "runs" / "20261103_204727_05_F0_trendstock_vsumn60__cord30_ksft"
MOM = "qlib158_vsumn60"
CONFIRM = ["qlib158_cord30", "qlib158_ksft"]
SPLITS = [("2024-01-01", "2025-06-30"), ("2025-07-01", "2026-11-03")]


def _slice(data_map: dict[str, pd.DataFrame], start: str, end: str) -> dict[str, pd.DataFrame]:
    out = {}
    for code, df in data_map.items():
        sub = df.loc[(df.index >= start) & (df.index <= end)]
        if len(sub) >= 120:
            out[code] = sub
    return out


def _attribution(run_dir: Path, data_map: dict[str, pd.DataFrame]) -> pd.DataFrame:
    pos = pd.read_csv(run_dir / "artifacts" / "positions.csv", index_col=0, parse_dates=True)
    close = pd.DataFrame({c: data_map[c]["close"] for c in data_map}).reindex(pos.index)
    rets = close.pct_change().fillna(0.0)
    contrib = pos.shift(1).fillna(0.0) * rets
    total = contrib.sum(axis=0).sort_values(ascending=False)
    gross = float(total.sum())
    return pd.DataFrame(
        {
            "code": total.index,
            "contrib": total.values,
            "share": total.values / gross if gross else np.zeros(len(total)),
            "days_held": (pos[total.index] > 0).sum().values,
        }
    )


def main() -> None:
    irs = _load_irs()
    data_map = _load_ohlcv_src(SRC_RUN)
    panel = _build_panel(data_map)
    close = panel["close"]
    fwd = close.pct_change().shift(-1)

    # Rebuild the winner scores exactly as stage 2 did.
    scores: dict[str, pd.DataFrame] = {}
    for aid in [MOM] + CONFIRM:
        raw = Registry().compute(aid, panel)
        raw = raw.reindex(index=close.index, columns=close.columns)
        stats = _pool_ic(raw, fwd)
        sign = -1.0 if stats["ic_mean"] < 0 else 1.0
        scores[aid] = _smooth_cs(raw * sign if sign < 0 else raw)
        print(f"[score] {aid} ic={stats['ic_mean']:+.4f} sign={sign:+.0f}")
    irs_eff = dict(irs)
    irs_eff.setdefault("qlib158_cord30", 0.128)
    irs_eff.setdefault("qlib158_ksft", 0.132)

    lines = ["", "## 稳健性检查", ""]
    lines += [
        "### 分段样本（同参数、独立区间重跑市场引擎）",
        "",
        "| 区间 | total | benchmark | excess | sharpe | max_dd | MC p_sharpe |",
        "|------|------:|----------:|-------:|-------:|-------:|------------:|",
    ]
    for start, end in SPLITS:
        sub = _slice(data_map, start, end)
        # Engine reads dates from config; monkeypatch the module-level constants.
        import scripts.research_trendstock_stage2 as S

        old = (S.START_DATE, S.END_DATE)
        S.START_DATE, S.END_DATE = start, end
        try:
            res = _run(
                MOM,
                CONFIRM,
                scores,
                irs_eff,
                sub,
                OUT_ROOT / f"split_{start}_{end}",
                1000,
            )
            m = res["metrics"]
            p = ((res.get("validation") or {}).get("monte_carlo") or {}).get("p_value_sharpe")
            lines.append(
                f"| {start} ~ {end} | {_pct(m.get('total_return'))} | "
                f"{_pct(m.get('benchmark_return'))} | {_pct(m.get('excess_return'))} | "
                f"{_num(m.get('sharpe'))} | {_pct(m.get('max_drawdown'))} | {_num(p)} |"
            )
        finally:
            S.START_DATE, S.END_DATE = old

    attr = _attribution(WINNER_RUN, data_map)
    names = _name_map()
    print(attr.to_string(index=False, max_rows=15))
    top5 = attr.head(5)
    lines += [
        "",
        "### 收益归因（全区间，前 5 名）",
        "",
        "| 代码 | 名称 | 累计贡献 | 占毛收益 | 持仓天数 |",
        "|------|------|--------:|--------:|--------:|",
    ]
    for row in top5.itertuples(index=False):
        lines.append(
            f"| {row.code} | {names.get(row.code, '')} | {_pct(row.contrib)} | "
            f"{_pct(row.share)} | {int(row.days_held)} |"
        )
    lines += [
        "",
        f"- 前 5 名合计占毛收益 {_pct(top5['share'].sum())}；正贡献标的 "
        f"{int((attr['contrib'] > 0).sum())} / {len(attr)}；负贡献合计 {_pct(attr[attr['contrib'] < 0]['contrib'].sum())}",
        "",
    ]
    out = OUT_ROOT / "robustness.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[done] {out}")


if __name__ == "__main__":
    raise SystemExit(main())
