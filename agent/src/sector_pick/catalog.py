"""Curated 「最优」 sector recipes used by the sector-pick board.

Each row points at the winning F0' or F1 run documented in
``agent/prop_国泰君安_*_最优.md``. Missing run directories fail-soft at scan
time instead of being dropped from this list.
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
        confirm_ids: Hard-confirm factor ids (IR-weighted when 2+).
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
        }

    def annotated_dict(self, *, runs_root: Path | None = None) -> dict:
        """Catalog row plus the Monitor-style run title for cross-checking."""
        payload = self.to_dict()
        title, present = load_run_display_title(self.run_id, runs_root=runs_root)
        payload["run_title"] = title
        payload["run_missing"] = not present
        return payload


# Confirm legs and run ids are copied from the matching 最优 markdown / formal
# rerun scripts. Do not "upgrade" a row to a newer run without updating the prop.
CATALOG: tuple[SectorRecipe, ...] = (
    SectorRecipe(
        key="robot",
        name="人形机器人",
        panel="人形机器人 BK1184",
        confirm_ids=("gtja191_124", "gtja191_178"),
        use_max_veto=False,
        run_id="20260725_184000_05_F0_robot_124_178",
        prop_file="prop_国泰君安_人形机器人_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="memory",
        name="存储芯片",
        panel="存储芯片 BK1137",
        confirm_ids=("gtja191_185", "gtja191_070"),
        use_max_veto=False,
        run_id="20260731_tech3_05_F0_memory_185_070",
        prop_file="prop_国泰君安_存储芯片_最优.md",
        variant="f0_prime",
    ),
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
    SectorRecipe(
        key="compute",
        name="算力硬件",
        panel="算力硬件（光模块∪PCB∪液冷）",
        confirm_ids=("gtja191_178", "gtja191_156"),
        use_max_veto=False,
        run_id="20260731_tech3_05_F0_compute_178_156",
        prop_file="prop_国泰君安_算力硬件_最优.md",
        variant="f0_prime",
    ),
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
    # Market-pick winner 2026-08-21: alive-factor confirm search → F0' 164+171/075.
    SectorRecipe(
        key="cro",
        name="医疗研发外包",
        panel="医疗研发外包 BK1600",
        confirm_ids=("gtja191_171", "gtja191_075"),
        use_max_veto=False,
        run_id="20260821_160000_03_F0_cro_171_075",
        prop_file="prop_国泰君安_医疗研发外包_最优.md",
        variant="f0_prime",
    ),
    # CRO 概念 BK0899: confirm search kept the 171/054 baseline (F1 subtracted).
    SectorRecipe(
        key="cro0899",
        name="CRO",
        panel="CRO BK0899",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=False,
        run_id="20260821_163600_03_F0_cro0899_171_054",
        prop_file="prop_国泰君安_CRO_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="speceq",
        name="其他专用设备",
        panel="其他专用设备 BK1405",
        confirm_ids=("gtja191_127", "gtja191_111"),
        use_max_veto=False,
        run_id="20260730_162800_03_F0_speceq_127_111",
        prop_file="prop_国泰君安_其他专用设备_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="aiapp",
        name="AI应用",
        panel="AI应用",
        confirm_ids=("gtja191_171",),
        use_max_veto=False,
        run_id="20260725_193311_05_F0_aiapp_171",
        prop_file="prop_国泰君安_AI应用_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="machine",
        name="工业母机",
        panel="工业母机 BK1004",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=False,
        run_id="20260726_084500_03_F0_machine_171_054",
        prop_file="prop_国泰君安_工业母机_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="vrar",
        name="虚拟现实",
        panel="虚拟现实 BK0722",
        confirm_ids=("gtja191_077",),
        use_max_veto=False,
        run_id="20260726_123500_03_F0_vrar_077",
        prop_file="prop_国泰君安_虚拟现实_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="autoparts",
        name="其他汽车零部件",
        panel="其他汽车零部件 BK1528",
        confirm_ids=("gtja191_100", "gtja191_081"),
        use_max_veto=False,
        run_id="20260730_162800_03_F0_autoparts_100_081",
        prop_file="prop_国泰君安_其他汽车零部件_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="copper",
        name="金属铜",
        panel="金属铜 BK1615",
        confirm_ids=("gtja191_081", "gtja191_160"),
        use_max_veto=False,
        run_id="20260726_131500_03_F0_copper_081_160",
        prop_file="prop_国泰君安_金属铜_最优.md",
        variant="f0_prime",
    ),
    SectorRecipe(
        key="tcm",
        name="中药Ⅲ",
        panel="中药Ⅲ BK1608",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260730_162800_02_F1_tcm",
        prop_file="prop_国泰君安_中药Ⅲ_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="agrochem",
        name="农化制品",
        panel="农化制品 BK0731",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260730_162800_02_F1_agrochem",
        prop_file="prop_国泰君安_农化制品_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="ac",
        name="空调",
        panel="空调 BK1450∪BK1400",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260729_143500_02_F1_ac",
        prop_file="prop_国泰君安_空调_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="snack",
        name="零食",
        panel="零食 BK1583",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260729_150000_02_F1_snack",
        prop_file="prop_国泰君安_零食_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="softdrink",
        name="软饮料",
        panel="软饮料 BK1586",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260729_184000_02_F1_softdrink",
        prop_file="prop_国泰君安_软饮料_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="broker",
        name="券商",
        panel="证券Ⅱ BK0473",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260729_135623_02_F1_broker",
        prop_file="prop_国泰君安_券商_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="truck",
        name="商用载货车",
        panel="商用载货车 BK1530",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260729_153000_02_F1_truck",
        prop_file="prop_国泰君安_商用载货车_最优.md",
        variant="f1_max",
    ),
    SectorRecipe(
        key="semeq",
        name="半导体设备材料",
        panel="半导体设备 BK1326 ∪ 半导体材料 BK1325",
        confirm_ids=("gtja191_171", "gtja191_054"),
        use_max_veto=True,
        run_id="20260731_tech3_02_F1_semeq",
        prop_file="prop_国泰君安_半导体设备材料_最优.md",
        variant="f1_max",
    ),
)

_BY_KEY = {row.key: row for row in CATALOG}

_BK_RE = re.compile(r"BK\d{4}", re.IGNORECASE)

# Catalog panels that are unions or have no single BK in ``panel``.
_EXTRA_BK: dict[str, tuple[str, ...]] = {
    "compute": ("BK1136", "BK1340", "BK1138"),
    "ac": ("BK1450", "BK1400"),
    "semeq": ("BK1326", "BK1325"),
}
_EXTRA_NAMES: dict[str, tuple[str, ...]] = {
    "aiapp": ("AI应用", "人工智能"),
    "compute": ("光通信模块", "印制电路板", "液冷概念", "算力硬件"),
}


def catalog_key_for_board(bk: str, name: str = "") -> str | None:
    """Map an Eastmoney board onto a researched catalog key, if any.

    Matches ``BKxxxx`` tokens in ``panel`` (including ∪ unions), plus the
    explicit aliases for compute / AC / semeq / AI-app. First catalog row wins.
    """
    code = str(bk or "").strip().upper()
    label = str(name or "").strip()
    for row in CATALOG:
        panel_codes = {token.upper() for token in _BK_RE.findall(row.panel)}
        extra_codes = {token.upper() for token in _EXTRA_BK.get(row.key, ())}
        names = {row.name, *_EXTRA_NAMES.get(row.key, ())}
        if code and code in panel_codes | extra_codes:
            return row.key
        if label and label in names:
            return row.key
    return None


def recipe_by_key(key: str) -> SectorRecipe | None:
    """Return the catalog row for ``key``, or None when unknown."""
    return _BY_KEY.get(str(key or "").strip())


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
    root = runs_root
    if root is None:
        from src.api.helpers import RUNS_DIR

        root = RUNS_DIR
    run_dir = Path(root) / rid
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
