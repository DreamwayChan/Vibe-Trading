# custom zoo

Hand-ported / research-derived alphas maintained in this repository.

## Absorb (legacy chart rewrite)

`custom_comp_low` and `custom_near_low` are clean-operator rewrites inspired by
community Tonghuashun/通达信 "主力吸货" formulas. They are **not** proprietary
THS indicators, do not claim Level-2 "main force" ground truth, and should be
treated as technical location/compression scores for research use only.

## Quality / flow confirms (preferred)

These prefer academically documented premia over chart "absorb" heuristics:

| ID | Role | Core idea |
|----|------|-----------|
| `custom_ivol_inv` | quality confirm | −idiosyncratic vol (Ang et al. 2006) |
| `custom_abn_to_inv` | liquidity confirm | −abnormal turnover 5d/60d (Datar / China low-TO) |
| `custom_max_inv` | sentiment confirm | −MAX lottery return (Bali et al. 2011) |
| `custom_vp_corr` | volume confirm | return–volume correlation |
| `custom_risk_mom` | momentum (alt sort) | 20d mean return / 20d vol |

All scores are raw (higher = better for the long leg). Cross-sectional
percentile / IR weighting belongs in `signal_engine.py`, not inside the zoo.
