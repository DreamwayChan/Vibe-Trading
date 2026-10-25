"""Window / smoothing tune for the top absorb candidates."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from scripts.research_custom_absorb import (  # noqa: E402
    ARTIFACTS,
    _ema,
    _ic_summary,
    _group_spread,
    _sma,
    _xs_corr_mean,
    fwd_return,
    load_panel,
)
from src.factors.base import safe_div, ts_max, ts_min  # noqa: E402
from src.factors.factor_analysis_core import (  # noqa: E402
    compute_group_equity,
    compute_ic_series,
)
from src.factors.registry import Registry  # noqa: E402


def main() -> int:
    panel = load_panel(ARTIFACTS)
    ret5 = fwd_return(panel["close"], 5)
    f164 = Registry().compute("gtja191_164", panel)
    c, h, l = panel["close"], panel["high"], panel["low"]

    variants: list[tuple[str, pd.DataFrame]] = []
    for low_n in (21, 38, 55):
        for short_n, long_n in ((5, 40), (10, 60), (20, 60), (10, 90)):
            near = 1.0 - safe_div(
                c - ts_min(l, low_n), ts_max(h, low_n) - ts_min(l, low_n)
            )
            compression = (
                1.0
                - safe_div(
                    ts_max(h, short_n) - ts_min(l, short_n),
                    ts_max(h, long_n) - ts_min(l, long_n),
                )
            ).clip(lower=0.0)
            raw = near * compression
            variants.append((f"comp_low L{low_n} C{short_n}/{long_n}", raw))
            variants.append(
                (f"comp_low L{low_n} C{short_n}/{long_n} ema3", _ema(raw, 3))
            )

    near38 = 1.0 - safe_div(c - ts_min(l, 38), ts_max(h, 38) - ts_min(l, 38))
    variants.append(("near_low_38", near38))
    variants.append(("near_low_38 ema5", _ema(near38, 5)))

    rsv = safe_div(c - ts_min(l, 21), ts_max(h, 21) - ts_min(l, 21)) * 100.0
    k = _sma(rsv, 13, 8)
    variants.append(("inv_slow_stoch", 100.0 - k))

    chg = l - l.shift(1)
    down_share = safe_div(_sma((-chg).clip(lower=0.0), 3, 1), _sma(chg.abs(), 3, 1))
    down_fade = 1.0 - down_share
    compression = (
        1.0
        - safe_div(ts_max(h, 10) - ts_min(l, 10), ts_max(h, 60) - ts_min(l, 60))
    ).clip(lower=0.0)
    variants.append(("comp_low_x_fade", near38 * compression * down_fade))
    variants.append(
        (
            "comp_low_x_sqrt_fade",
            near38 * compression * np.sqrt(down_fade.clip(lower=0.0)),
        )
    )
    variants.append(
        ("comp_low_gated", (near38 * compression).where(near38 >= 0.5, 0.0))
    )

    # Relative-position after compression, inverted for long (low after coil).
    rsv21 = safe_div(c - ts_min(l, 21), ts_max(h, 21) - ts_min(l, 21))
    variants.append(("inv_comp_pos", (1.0 - rsv21) * compression))
    variants.append(("inv_comp_pos ema3", _ema((1.0 - rsv21) * compression, 3)))

    rows: list[tuple] = []
    for name, factor in variants:
        ic = compute_ic_series(factor, ret5)
        summary = _ic_summary(ic)
        equity = compute_group_equity(factor, ret5, n_groups=5)
        group = _group_spread(equity)
        corr = _xs_corr_mean(factor, f164)
        if np.isfinite(summary["ic_mean"]) and np.isfinite(corr):
            score = abs(summary["ic_mean"]) * (1.0 - min(abs(corr), 1.0))
        else:
            score = float("nan")
        rows.append((score, name, summary, group, corr))

    rows.sort(key=lambda x: x[0] if np.isfinite(x[0]) else -1.0, reverse=True)
    print("TOP 20 by complement_score")
    for score, name, summary, group, corr in rows[:20]:
        print(
            f"{name:40s} IC5={summary['ic_mean']:+.4f} IR={summary['ir']:+.3f} "
            f"pos={summary['ic_pos_ratio']:.3f} "
            f"spread={group['long_short_spread']:+.2f} "
            f"mono={group['mono_score']:+.2f} "
            f"corr164={corr:+.3f} comp={score:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
