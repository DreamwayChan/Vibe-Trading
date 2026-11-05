"""Assemble the final GTJA191 deliverables (summary + published-run report + prop).

Run from ``agent/`` after the three analysis steps:
    python scripts/finalize_gtja_report.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

OUT = _AGENT_ROOT / "runs" / "_research_gtja_trendstock_confirm_search"


def _read(name: str) -> str:
    p = OUT / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def main() -> None:
    runs = sorted((_AGENT_ROOT / "runs").glob("*_05_F0_trendstock_gtja_*"))
    run = runs[-1]
    ctx = json.loads((run / "req.json").read_text(encoding="utf-8"))["context"]
    m = pd.read_csv(run / "artifacts" / "metrics.csv").iloc[0]
    val = json.loads((run / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    p_mc = ((val.get("monte_carlo") or {}).get("p_value_sharpe"))

    delta = pd.read_csv(OUT / "gtja_confirm_delta_agg.csv", index_col=0)
    rob = _read("robustness_confirm.md")
    dlt = _read("robustness_delta.md")
    rob_main = _read("robustness_gtja.md")

    # ---- 1. summary: append the robustness blocks with renumbered headings
    summary = OUT / "gtja_summary.md"
    text = summary.read_text(encoding="utf-8")
    text = text.split("\n---\n**稳健性")[0]
    rob_main = (
        rob_main.replace("### 1. 分段样本", "### 7.1 分段样本")
        .replace("### 2. 确认腿阶梯", "### 7.2 确认腿阶梯")
        .replace("### 3. 收益归因", "### 7.3 收益归因")
        .replace("## 稳健性检查（GTJA191 存活因子）", "## 7. 稳健性检查")
    )
    rob = rob.replace("### 4. 确认腿稳健性", "## 8. 确认腿跨动量腿稳健性")
    dlt = dlt.replace("### 5. 确认腿的边际价值", "## 9. 确认腿的边际价值（真正的“最优确认脚”）")

    best_robust = delta.index[0]
    best_mean_delta = float(delta.iloc[0]["mean_delta"])
    best_min = float(delta.iloc[0]["min_delta"])
    best_pos = int(delta.iloc[0]["pos_ratio"] * delta.iloc[0]["n"])
    n_all = int(delta.iloc[0]["n"])
    d_002168 = delta.loc["002+168"] if "002+168" in delta.index else None

    conclusion = f"""
---

## 10. 结论

- **最优回测（excess 口径，已发布）**：动量腿 `{ctx['mom_id'].replace('gtja191_', '')}` + 确认腿
  `{'/'.join(a.replace('gtja191_', '') for a in ctx['confirm_ids'])}`，
  excess **{m.excess_return:.2%}**、total **{m.total_return:.2%}**、sharpe **{m.sharpe:.4f}**、
  max_dd **{m.max_drawdown:.2%}**、MC p_sharpe **{p_mc:.4f}**。
- **最优确认脚（稳健口径）**：`{best_robust}` —— 换动量腿后平均 Δexcess
  **{best_mean_delta:+.2%}**、最差 {best_min:+.2%}、正增益 {best_pos}/{n_all} 条动量腿。
- 两者不一致，必须说清楚：`002+168` 的高 excess 主要来自动量腿 `124` 本身（pure 已有
  {float(pd.read_csv(OUT / 'gtja_pure_baseline.csv', index_col=0).loc['gtja191_124'].iloc[0]):+.2%}），
  它在别的动量腿上的平均边际价值只有
  **{float(d_002168['mean_delta']):+.2%}**（正增益 {int(float(d_002168['pos_ratio']) * int(float(d_002168['n'])))}/{int(float(d_002168['n']))}）。
  换言之：`002+168` 是“单格最优”，`{best_robust}` 才是“可迁移的确认脚”。
- **GTJA191 存活集没有打败 qlib158 冠军**：{m.excess_return:.2%} vs 63.92%（-{(0.6392 - float(m.excess_return)):.2%}），
  sharpe 1.86 vs 2.32；但 MC 显著性更好（p {p_mc:.3f} vs 0.543）。
- **稳定性**：分段 excess 2024 +36.77% → 2025 +5.05% → 2026 -5.24%，逐年衰减。
- **集中度**：前 5 只贡献毛收益 81.36%，正贡献 29/80。
- 研究/回测，非实盘建议。
"""
    summary.write_text(text.rstrip() + "\n" + rob_main + "\n" + rob + "\n" + dlt + conclusion, encoding="utf-8")
    print(f"[summary] {summary}")

    # ---- 2. published run report: append robustness digest
    rep = next(run.glob("REPORT_*.md"))
    body = rep.read_text(encoding="utf-8")
    body = body.split("\n## 稳健性")[0]
    digest = f"""
## 稳健性（研究记录，详见 `runs/_research_gtja_trendstock_confirm_search/gtja_summary.md`）

| 检查 | 结果 |
|---|---|
| 分段 excess（2024 / 2025 / 2026YTD） | +36.77% / +5.05% / **-5.24%** |
| 确认腿阶梯（同一动量腿） | pure +21.80% → 只 002 +15.33% → 只 168 +32.23% → **002+168 +58.80%** |
| 同动量腿换 qlib158 确认腿（cord30+ksft） | +28.51%（Δ {0.5880 - 0.2851:+.2%}） |
| 确认腿跨动量腿边际价值（`002+168`） | 平均 {float(d_002168['mean_delta']):+.2%}，正增益 {int(float(d_002168['pos_ratio']) * int(float(d_002168['n'])))}/{int(float(d_002168['n']))} → **不可迁移** |
| 真正可迁移的确认腿 | `{best_robust}`：平均 {best_mean_delta:+.2%}，最差 {best_min:+.2%}，{best_pos}/{n_all} 正增益 |
| 收益集中度 | 前 5 只占毛收益 81.36%，正贡献 29/80 |

**结论**：本记录是 excess 口径下的单格最优，MC p={p_mc:.3f} 仍 > 0.2，且确认腿在换动量腿后退化；
若要部署，应使用稳健确认腿 `{best_robust}` 而不是 `002+168`。
"""
    rep.write_text(body.rstrip() + "\n" + digest, encoding="utf-8")
    print(f"[report] {rep}")

    # ---- 3. prop doc
    prop = _AGENT_ROOT / "prop_gtja191_趋势股_最优.md"
    text = prop.read_text(encoding="utf-8").split("\n## 稳健性")[0].rstrip()
    text += f"""

## 稳健性（务必一起看）

| 检查 | 结果 |
|---|---|
| 分段 excess 2024/2025/2026YTD | +36.77% / +5.05% / -5.24% |
| 确认腿阶梯 | pure +21.80% → 002 +15.33% → 168 +32.23% → 002+168 +58.80% |
| 确认腿跨动量腿边际（002+168） | 平均 {float(d_002168['mean_delta']):+.2%}，正增益 {int(float(d_002168['pos_ratio']) * int(float(d_002168['n'])))}/{int(float(d_002168['n']))} |
| 最优可迁移确认腿 | `{best_robust}`：平均 {best_mean_delta:+.2%}，最差 {best_min:+.2%}，{best_pos}/{n_all} |
| 收益集中度 | 前 5 只 81.36%，正贡献 29/80 |
| MC p_sharpe | {p_mc:.3f}（>0.2，不显著） |

## 文件

- run 记录：`runs/{run.name}`
- 搜索与稳健性：`runs/_research_gtja_trendstock_confirm_search/gtja_summary.md`
- 脚本：`scripts/research_gtja_trendstock_confirm_search.py` / `analyze_gtja_winner.py` / `ablate_gtja_confirm.py` / `delta_gtja_confirm.py` / `finalize_gtja_report.py`
"""
    prop.write_text(text + "\n", encoding="utf-8")
    print(f"[prop] {prop}")


if __name__ == "__main__":
    raise SystemExit(main())
