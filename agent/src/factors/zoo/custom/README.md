# `custom` zoo — research only

These seven factors (`custom_abn_to_inv`, `custom_comp_low`, `custom_ivol_inv`,
`custom_max_inv`, `custom_near_low`, `custom_risk_mom`, `custom_vp_corr`) were
written for the frozen sector-prompt experiments under `agent/prop_*.md`.

They are **not** on the default path. That path is lake → hypothesis →
cost-aware backtest → validation gate → tradable → paper book. See
[CAPABILITY.md](../../../../../CAPABILITY.md).

`alpha bench` / `alpha show` still load them so old research can be read.
Do not treat a high in-sample score here as a tradable signal.
