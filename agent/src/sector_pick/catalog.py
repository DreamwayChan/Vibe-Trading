"""Curated 「最优」 sector recipes used by the sector-pick board.

Research-only. Every row here must point at a backtest that still exists under
``agent/runs/<run_id>``. The shelf is rebuilt from the report library (runs
that carry ``artifacts/metrics.csv``), because a row whose run directory was
deleted only ever renders 「回测缺失」 on the board — it cannot be scored, and
it teaches nothing about the sector.

When a theme has more than one surviving report, the row keeps the current
price basis (``qfq``) and the full window; the archived duplicate is not
resurrected. Three rows (创新药 / 食品饮料 / 存储芯片) only have an archived
report left — they stay on the shelf as research references and are marked as
such in the row comment.

This is not the default path. That path is lake → hypothesis → cost-aware
backtest → validation gate → tradable → paper book (see ``CAPABILITY.md``).

Missing run directories still fail soft at scan time instead of being dropped
from this list; ``missing_run_keys`` reports those keys so a deletion surfaces
in the server log instead of silently rotting the board.

Because rows follow the surviving reports, a row can end up pointing at a run a
monitor board is bound to. That does not demote the board: the shelf is a
display index, and admission only treats a row as a frozen recipe when the run
carries no decision record of its own (``src.monitor.uncausal``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Variant = Literal["f0_prime", "f1_max"]


@dataclass(frozen=True)
class SectorRecipe:
    """One historically validated sector strategy.

    Attributes:
        key: Stable id used in APIs and persisted enabled-set.
        name: Display name (Chinese board label).
        panel: Eastmoney board / universe description.
        confirm_ids: Hard-confirm factor ids (IR-weighted when 2+; empty =
            momentum-only recipe, confirm coverage reads 1.0).
        use_max_veto: True when the published winner is F1 (+ custom_max_inv).
        run_id: ``agent/runs/<run_id>`` that holds signal_engine.py + universe.
        prop_file: Research prompt markdown that named this winner.
        variant: ``f0_prime`` (sector-specific confirms) or ``f1_max``.
    """

    key: str
    name: str
    panel: str
    confirm_ids: tuple[str, ...]
    use_max_veto: bool
    run_id: str
    prop_file: str
    variant: Variant

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "panel": self.panel,
            "confirm_ids": list(self.confirm_ids),
            "use_max_veto": self.use_max_veto,
            "run_id": self.run_id,
            "prop_file": self.prop_file,
            "variant": self.variant,
            "research_only": True,
        }

    def annotated_dict(self, *, runs_root: Path | None = None) -> dict:
        """Catalog row plus the Monitor-style run title for cross-checking."""
        payload = self.to_dict()
        title, present = load_run_display_title(self.run_id, runs_root=runs_root)
        payload["run_title"] = title
        payload["run_missing"] = not present
        return payload


# Confirm legs and run ids are copied from the matching 最优 markdown / formal
# rerun scripts. Do not "upgrade" a row to a newer run without updating the prop,
# and do not add a row whose run directory does not exist.
CATALOG: tuple[SectorRecipe, ...] = (
    # CRO 医疗研发外包: qfq 全窗重跑（对照已归档的 H4 原始价板）。
    # +215.87% / 超额 +168.82%，验证门四项全过。
    SectorRecipe(
        key="cro",
        name="医疗研发外包",
        panel="医疗研发外包 BK1600",
        confirm_ids=("gtja191_171", "gtja191_075"),
        use_max_veto=False,
        run_id="20261111_105004_05_H6_cro_171_075_qfq",
        prop_file="prop_国泰君安_医疗研发外包_最优.md",
        variant="f0_prime",
    ),
    # 金属铜: 冻结配方 081+160，qfq 全窗。+113.68% / 超额 +12.13%，四项过门。
    SectorRecipe(
        key="copper",
        name="金属铜",
        panel="金属铜 BK1615",
        confirm_ids=("gtja191_081", "gtja191_160"),
        use_max_veto=False,
        run_id="20261111_111500_05_H8_copper_081_160_qfq",
        prop_file="prop_国泰君安_金属铜_最优.md",
        variant="f0_prime",
    ),
    # 虚拟现实: 第二轮单变量 mom_only（确认腿全剔，只按 164 排序）。
    # run 四项过门（+84.57% / 超额 +47.63%），但事前失效条件第三条使假设判
    # rejected —— run 合规 ≠ 假设成立。
    SectorRecipe(
        key="vrar",
        name="虚拟现实",
        panel="虚拟现实 BK0722",
        confirm_ids=(),
        use_max_veto=False,
        run_id="20261111_133500_05_H21_vrar_164only",
        prop_file="prop_国泰君安_虚拟现实_最优.md",
        variant="f0_prime",
    ),
    # 趋势股 BK1715: Qlib158 因子组合（vsumn60 动量 + cord30/ksft 确认）。
    # 旧方案（原始价、仅 monte_carlo 门、窗口止于 2026-11-03）里唯一还活着的
    # 趋势股报告；保留作为该板块的研究参照。
    SectorRecipe(
        key="trendstock",
        name="趋势股",
        panel="趋势股 BK1715",
        confirm_ids=("qlib158_cord30", "qlib158_ksft"),
        use_max_veto=False,
        run_id="20261103_204727_05_F0_trendstock_vsumn60__cord30_ksft",
        prop_file="prop_qlib158_趋势股_最优.md",
        variant="f0_prime",
    ),
    # 人形机器人: 第二轮单变量 no_timing（关掉 MA20 半仓）。
    # +41.36% / 超额 -33.87%，门不过；板块大涨而策略跑输，是仓位端损耗的证据。
    SectorRecipe(
        key="robot",
        name="人形机器人",
        panel="人形机器人 BK1184",
        confirm_ids=("gtja191_124", "gtja191_178"),
        use_max_veto=False,
        run_id="20261111_130500_05_H18_robot_notiming",
        prop_file="prop_国泰君安_人形机器人_最优.md",
        variant="f0_prime",
    ),
    # 创新药: 旧方案唯一一份报告（原始价、短窗，+14.22% / 超额 +3.75%）。
    # run 已归档，仅作该板块的研究参照，不代表可交易资格。
    SectorRecipe(
        key="drug",
        name="创新药",
        panel="创新药 BK1106",
        confirm_ids=("gtja191_129",),
        use_max_veto=False,
        run_id="20260725_184000_05_F0_drug_129",
        prop_file="prop_国泰君安_创新药_最优.md",
        variant="f0_prime",
    ),
    # 食品饮料: 旧方案唯一一份报告（原始价、短窗，+43.92% / 超额 +57.89%）。
    # run 已归档，仅作研究参照。
    SectorRecipe(
        key="foodbev",
        name="食品饮料",
        panel="食品饮料 BK0438",
        confirm_ids=("gtja191_127",),
        use_max_veto=False,
        run_id="20260730_162800_03_F0_foodbev_127",
        prop_file="prop_国泰君安_食品饮料_最优.md",
        variant="f0_prime",
    ),
    # 存储芯片: 第二轮单变量去 185，只留 164+070。qfq 全窗 +96.24%，
    # 但超额 -71.94% —— 门过而不跑赢基准，归档；该板块现无更好的存活报告。
    SectorRecipe(
        key="memory",
        name="存储芯片",
        panel="存储芯片 BK1137",
        confirm_ids=("gtja191_070",),
        use_max_veto=False,
        run_id="20261111_134500_05_H22_memory_164_070",
        prop_file="prop_国泰君安_存储芯片_最优.md",
        variant="f0_prime",
    ),
)

_BY_KEY = {row.key: row for row in CATALOG}

_BK_RE = re.compile(r"BK\d{4}", re.IGNORECASE)


def catalog_key_for_board(bk: str, name: str = "") -> str | None:
    """Map an Eastmoney board onto a researched catalog key, if any.

    Matches ``BKxxxx`` tokens in ``panel`` (a ``∪`` union lists several) or the
    row's display name. First catalog row wins.
    """
    code = str(bk or "").strip().upper()
    label = str(name or "").strip()
    for row in CATALOG:
        panel_codes = {token.upper() for token in _BK_RE.findall(row.panel)}
        if code and code in panel_codes:
            return row.key
        if label and label == row.name:
            return row.key
    return None


def recipe_by_key(key: str) -> SectorRecipe | None:
    """Return the catalog row for ``key``, or None when unknown."""
    return _BY_KEY.get(str(key or "").strip())


def _runs_root(runs_root: Path | None) -> Path:
    if runs_root is not None:
        return Path(runs_root)
    from src.api.helpers import RUNS_DIR

    return Path(RUNS_DIR)


def load_run_display_title(
    run_id: str,
    *,
    runs_root: Path | None = None,
) -> tuple[str | None, bool]:
    """Read the Monitor picker title for ``run_id``.

    Same source as the strategy-source list: ``req.json`` ``title`` (the
    rename the user saved on the Monitor page), then the prompt first line.
    ``present`` is True only when ``code/signal_engine.py`` exists.

    Returns:
        ``(title, present)``. Title is None when the run dir is missing or
        has no usable label.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None, False
    run_dir = _runs_root(runs_root) / rid
    present = (run_dir / "code" / "signal_engine.py").is_file()
    req_path = run_dir / "req.json"
    if not req_path.is_file():
        return None, present
    try:
        raw = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, present
    if not isinstance(raw, dict):
        return None, present
    stored = raw.get("title")
    if isinstance(stored, str) and stored.strip():
        return stored.strip(), present
    prompt = raw.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip().splitlines()[0][:200], present
    return None, present


def missing_run_keys(*, runs_root: Path | None = None) -> list[str]:
    """Catalog keys whose run directory (or signal engine) is gone.

    A deleted backtest cannot be scored: the row only renders 「回测缺失」.
    Callers log this instead of letting the board rot silently.
    """
    root = _runs_root(runs_root)
    missing: list[str] = []
    for row in CATALOG:
        engine = root / row.run_id / "code" / "signal_engine.py"
        if not engine.is_file():
            missing.append(row.key)
    return missing


def default_enabled_keys() -> list[str]:
    """All catalog keys, in catalog order (page default = fully opted in)."""
    return [row.key for row in CATALOG]


def validate_enabled_keys(keys: list[str] | None) -> list[str]:
    """Deduplicate, drop blanks, reject unknown keys.

    Returns:
        Catalog keys in catalog order.

    Raises:
        ValueError: A key is not in the curated catalog.
    """
    if keys is None:
        return default_enabled_keys()
    wanted: set[str] = set()
    for raw in keys:
        key = str(raw or "").strip()
        if not key:
            continue
        if key not in _BY_KEY:
            raise ValueError(f"unknown sector key: {key!r}")
        wanted.add(key)
    return [row.key for row in CATALOG if row.key in wanted]
