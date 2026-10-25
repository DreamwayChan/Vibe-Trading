"""Repair invalid gtja191 alphas so they pass the CSI300 2023-2026 alive gate.

Phases (plan Fix GTJA191 survivors):
  reversed  — sign-flip (retry: flip + cross-sectional rank)
  near_miss — dead with ic_mean > 0.02; up to 3 variants
  dead      — remaining dead by |IC| desc; skip |IC|<0.01 after failed flip

Edits are in-place under src/factors/zoo/gtja191/: original compute is renamed
to ``_compute_raw`` and a thin ``compute`` wrapper applies the repair.
``__alpha_meta__["notes"]`` and the Chinese header keep the original formula
rationale.

Default is dry-run. Pass ``--apply`` to write files.

Run from agent/:
    python scripts/repair_gtja191_survival.py --phase reversed
    python scripts/repair_gtja191_survival.py --phase near_miss --apply
    python scripts/repair_gtja191_survival.py --phase dead --apply
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from src.factors.base import decay_linear, rank, ts_mean  # noqa: E402
from src.factors.bench_runner import categorise, t_stat  # noqa: E402
from src.factors.factor_analysis_core import compute_ic_series  # noqa: E402
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import survival_path  # noqa: E402
from src.tools.alpha_bench_tool import (  # noqa: E402
    _compute_forward_returns,
    _load_universe_panel,
)

ZOO_DIR = _AGENT_ROOT / "src" / "factors" / "zoo" / "gtja191"
LOG_PATH = _AGENT_ROOT / "scripts" / "repair_gtja191_survival_log.json"

UNIVERSE = "csi300"
PERIOD = "2023-2026"
REPAIR_TAG = "REPAIR CSI300 2023-2026"


@dataclass
class EvalRow:
    """One variant evaluation for a single alpha."""

    id: str
    phase: str
    attempt: str
    category: str
    ic_mean: float
    ic_positive_ratio: float
    t: float
    ir: float
    action: str  # would_apply | applied | abandon | skip


Transform = Callable[[pd.DataFrame], pd.DataFrame]


def _num_from_id(alpha_id: str) -> int:
    return int(alpha_id.rsplit("_", 1)[-1])


def _module_path(alpha_id: str) -> Path:
    return ZOO_DIR / f"alpha_{_num_from_id(alpha_id):03d}.py"


def _load_survival() -> dict[str, dict[str, Any]]:
    path = survival_path()
    if not path.is_file():
        raise FileNotFoundError(f"missing survival store: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or {}
    return {
        aid: row
        for aid, row in alphas.items()
        if str(aid).startswith("gtja191_") and isinstance(row, dict)
    }


def _ic_metrics(factor_df: pd.DataFrame, return_df: pd.DataFrame) -> dict[str, float]:
    ic = compute_ic_series(factor_df, return_df)
    if ic.empty:
        return {
            "ic_mean": 0.0,
            "ic_std": 0.0,
            "ir": 0.0,
            "ic_positive_ratio": 0.0,
            "ic_count": 0,
        }
    ic_mean = float(ic.mean())
    ic_std = float(ic.std())
    ir = ic_mean / ic_std if ic_std > 0 else 0.0
    return {
        "ic_mean": round(ic_mean, 6),
        "ic_std": round(ic_std, 6),
        "ir": round(ir, 4),
        "ic_positive_ratio": round(float((ic > 0).mean()), 4),
        "ic_count": int(len(ic)),
    }


def _evaluate(
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
) -> tuple[str, dict[str, float], float]:
    metrics = _ic_metrics(factor_df, return_df)
    cat = categorise(metrics)
    t = t_stat(metrics["ic_mean"], metrics["ic_std"], int(metrics["ic_count"]))
    return cat, metrics, t


def _transform_neg(df: pd.DataFrame) -> pd.DataFrame:
    return -df


def _transform_neg_rank(df: pd.DataFrame) -> pd.DataFrame:
    return rank(-df)


def _transform_rank(df: pd.DataFrame) -> pd.DataFrame:
    return rank(df)


def _transform_smooth_rank(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    # Smooth first, then CS-rank — lifts IC+ ratio when raw IC is borderline.
    return rank(ts_mean(df, window))


def _transform_decay_rank(df: pd.DataFrame, window: int = 8) -> pd.DataFrame:
    return rank(decay_linear(df, window))


def _transform_neg_smooth_rank(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    return rank(ts_mean(-df, window))


def _already_repaired(src: str) -> bool:
    return REPAIR_TAG in src or "def _compute_raw(" in src


def _update_notes_literal(tree: ast.Module, note: str) -> bool:
    """Set __alpha_meta__['notes'] string literal to ``note`` (AST rewrite)."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__alpha_meta__" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for i, key in enumerate(node.value.keys):
            if isinstance(key, ast.Constant) and key.value == "notes":
                node.value.values[i] = ast.Constant(value=note)
                return True
        # notes key missing — append
        node.value.keys.append(ast.Constant(value="notes"))
        node.value.values.append(ast.Constant(value=note))
        return True
    return False


def _patch_chinese_header(src: str, attempt: str) -> str:
    """Annotate the 简要说明 line so humans see the repair at a glance."""
    marker = f"[已修复/{attempt}] "
    if marker in src or REPAIR_TAG in src.split("\n", 20)[0:20].__repr__():
        # Still try to patch the 简要说明 once.
        pass
    pattern = r"(# 简要说明:\s*)(.*)"
    match = re.search(pattern, src)
    if not match:
        return src
    old = match.group(2).strip()
    if old.startswith("[已修复"):
        return src
    replacement = f"{match.group(1)}{marker}{old}"
    return src[: match.start()] + replacement + src[match.end() :]


def _build_wrapper_source(attempt: str) -> str:
    """Python source for the new ``compute`` wrapper."""
    if attempt == "sign_flip":
        body = "return -_compute_raw(panel)"
    elif attempt == "sign_flip_rank":
        body = "return rank(-_compute_raw(panel))"
    elif attempt == "cs_rank":
        body = "return rank(_compute_raw(panel))"
    elif attempt == "smooth5_rank":
        body = "return rank(ts_mean(_compute_raw(panel), 5))"
    elif attempt == "decay8_rank":
        body = "return rank(decay_linear(_compute_raw(panel), 8))"
    elif attempt == "sign_flip_smooth5_rank":
        body = "return rank(ts_mean(-_compute_raw(panel), 5))"
    else:
        raise ValueError(f"unknown attempt: {attempt}")

    needs_rank = "rank" in attempt or attempt == "cs_rank"
    needs_ts_mean = "smooth" in attempt
    needs_decay = "decay" in attempt
    imports: list[str] = []
    if needs_rank:
        imports.append("rank")
    if needs_ts_mean:
        imports.append("ts_mean")
    if needs_decay:
        imports.append("decay_linear")
    import_line = ""
    if imports:
        import_line = f"from src.factors.base import {', '.join(imports)}\n    "

    return textwrap.dedent(
        f'''

def compute(panel):
    """Repaired wrapper — polarity/transform for CSI300 2023-2026 survival.

    Original formula lives in ``_compute_raw``. See ``__alpha_meta__["notes"]``.
    """
    {import_line}{body}
'''
    )


def _replace_notes_in_source(src: str, note: str) -> str:
    """Replace or insert ``notes`` inside ``__alpha_meta__`` without unparsing."""
    # Single-line notes: 'notes': '...' or "notes": "..."
    single = re.compile(
        r"(^[ \t]*['\"]notes['\"]\s*:\s*)(['\"])(.*?)(\2)(\s*,?\s*)$",
        flags=re.M,
    )
    if single.search(src):
        # Use repr() so embedded quotes/backslashes stay valid Python literals.
        return single.sub(
            lambda m: f"{m.group(1)}{note!r},",
            src,
            count=1,
        )

    # Multiline / empty — fall back to AST splice of the meta dict only via
    # line-oriented insert before the closing brace of __alpha_meta__.
    meta_match = re.search(r"__alpha_meta__\s*=\s*\{", src)
    if not meta_match:
        raise RuntimeError("no __alpha_meta__ dict found")
    # Find matching closing brace from meta start.
    start = meta_match.end() - 1
    depth = 0
    end = None
    for i, ch in enumerate(src[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise RuntimeError("unclosed __alpha_meta__")
    # If notes key exists anywhere in the block, rewrite via AST on that slice.
    block = src[meta_match.start() : end + 1]
    if re.search(r"['\"]notes['\"]\s*:", block):
        tree = ast.parse(block + "\n")
        if not _update_notes_literal(tree, note):
            raise RuntimeError("notes AST update failed")
        new_block = ast.unparse(tree)
        return src[: meta_match.start()] + new_block + src[end + 1 :]
    insert = f"\n    'notes': {note!r},"
    return src[:end] + insert + src[end:]


def apply_repair(
    alpha_id: str,
    attempt: str,
    original_ic: float,
    original_category: str,
    new_metrics: dict[str, float],
) -> None:
    """Rewrite alpha module: rename compute → _compute_raw, add wrapper + notes."""
    path = _module_path(alpha_id)
    src = path.read_text(encoding="utf-8")
    if _already_repaired(src):
        raise RuntimeError(f"{alpha_id}: already repaired, refusing to double-patch")
    if re.search(r"^def compute\(", src, flags=re.M) is None:
        raise RuntimeError(f"{alpha_id}: cannot find def compute(")

    note = (
        f"{REPAIR_TAG}: original {original_category} (ic_mean={original_ic:.4f}); "
        f"applied {attempt} → alive "
        f"(ic_mean={new_metrics['ic_mean']:.4f}, "
        f"ic+={new_metrics['ic_positive_ratio']:.3f}, "
        f"ir={new_metrics['ir']:.3f}). "
        f"Original formula preserved in _compute_raw."
    )

    # Header comment stays via string edit; AST unparse would strip it.
    src = _patch_chinese_header(src, attempt)
    src = _replace_notes_in_source(src, note)
    src = re.sub(r"^def compute\(", "def _compute_raw(", src, count=1, flags=re.M)
    src = src.rstrip() + "\n" + _build_wrapper_source(attempt)
    ast.parse(src)  # syntax guard before write
    path.write_text(src, encoding="utf-8")


def _fresh_registry() -> Registry:
    # Drop cached modules so post-apply recompute sees new source.
    doomed = [
        name
        for name in list(sys.modules)
        if name.startswith("src.factors.zoo.gtja191.")
    ]
    for name in doomed:
        del sys.modules[name]
    return Registry()


def _candidates(
    survival: dict[str, dict[str, Any]],
    phase: str,
) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for aid, row in survival.items():
        cat = row.get("category")
        ic = float(row.get("ic_mean") or 0.0)
        if phase == "reversed":
            if cat == "reversed":
                rows.append((aid, row))
        elif phase == "near_miss":
            if cat == "dead" and ic > 0.02:
                rows.append((aid, row))
        elif phase == "dead":
            if cat == "dead" and ic <= 0.02:
                rows.append((aid, row))
        else:
            raise ValueError(phase)
    if phase == "reversed":
        rows.sort(key=lambda x: float(x[1].get("ic_mean") or 0.0))
    elif phase == "near_miss":
        rows.sort(key=lambda x: -float(x[1].get("ic_mean") or 0.0))
    else:
        rows.sort(key=lambda x: -abs(float(x[1].get("ic_mean") or 0.0)))
    return rows


def _attempts_for_phase(phase: str, original_ic: float) -> list[tuple[str, Transform]]:
    if phase == "reversed":
        return [
            ("sign_flip", _transform_neg),
            ("sign_flip_rank", _transform_neg_rank),
        ]
    # dead / near_miss: try polarity first when IC is negative, then stabilize.
    attempts: list[tuple[str, Transform]] = []
    if original_ic < 0:
        attempts.append(("sign_flip", _transform_neg))
        attempts.append(("sign_flip_rank", _transform_neg_rank))
        attempts.append(("sign_flip_smooth5_rank", _transform_neg_smooth_rank))
    else:
        attempts.append(("cs_rank", _transform_rank))
        attempts.append(("smooth5_rank", _transform_smooth_rank))
        attempts.append(("decay8_rank", _transform_decay_rank))
    return attempts[:3]


def repair_phase(
    phase: str,
    *,
    apply: bool,
    limit: int | None = None,
) -> list[EvalRow]:
    """Run one repair phase; optionally write successful patches."""
    survival = _load_survival()
    cands = _candidates(survival, phase)
    if limit is not None:
        cands = cands[:limit]

    print(f"Loading panel {UNIVERSE} {PERIOD} …")
    panel = _load_universe_panel(UNIVERSE, PERIOD)
    return_df = _compute_forward_returns(panel)
    reg = _fresh_registry()

    results: list[EvalRow] = []
    n_alive = 0
    n_abandon = 0

    for aid, row in cands:
        path = _module_path(aid)
        if not path.is_file():
            results.append(
                EvalRow(
                    id=aid,
                    phase=phase,
                    attempt="-",
                    category="missing",
                    ic_mean=0.0,
                    ic_positive_ratio=0.0,
                    t=0.0,
                    ir=0.0,
                    action="skip",
                )
            )
            continue
        src = path.read_text(encoding="utf-8")
        if _already_repaired(src):
            results.append(
                EvalRow(
                    id=aid,
                    phase=phase,
                    attempt="-",
                    category=str(row.get("category")),
                    ic_mean=float(row.get("ic_mean") or 0.0),
                    ic_positive_ratio=0.0,
                    t=0.0,
                    ir=float(row.get("ir") or 0.0),
                    action="skip",
                )
            )
            print(f"  SKIP {aid}: already repaired")
            continue

        orig_ic = float(row.get("ic_mean") or 0.0)
        orig_cat = str(row.get("category") or "dead")

        # Noise floor for broad dead phase: only try flip once if |IC|<0.01.
        if phase == "dead" and abs(orig_ic) < 0.01:
            attempts = [("sign_flip", _transform_neg)]
        else:
            attempts = _attempts_for_phase(phase, orig_ic)

        try:
            raw = reg.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {aid}: compute error: {exc}")
            results.append(
                EvalRow(
                    id=aid,
                    phase=phase,
                    attempt="compute",
                    category="error",
                    ic_mean=0.0,
                    ic_positive_ratio=0.0,
                    t=0.0,
                    ir=0.0,
                    action="abandon",
                )
            )
            n_abandon += 1
            continue

        won: tuple[str, dict[str, float], float] | None = None
        last_metrics: dict[str, float] | None = None
        last_attempt = attempts[-1][0] if attempts else "-"
        last_t = 0.0
        last_cat = "dead"

        for attempt_name, transform in attempts:
            cat, metrics, t = _evaluate(transform(raw), return_df)
            last_metrics, last_attempt, last_t, last_cat = metrics, attempt_name, t, cat
            print(
                f"  try {aid} {attempt_name}: cat={cat} "
                f"ic={metrics['ic_mean']:.4f} pos={metrics['ic_positive_ratio']:.3f} "
                f"t={t:.2f} ir={metrics['ir']:.3f}"
            )
            if cat == "alive":
                won = (attempt_name, metrics, t)
                break

        if won is None:
            # For |IC|<0.01 dead: flip failed → abandon without more tries.
            m = last_metrics or {
                "ic_mean": 0.0,
                "ic_positive_ratio": 0.0,
                "ir": 0.0,
            }
            results.append(
                EvalRow(
                    id=aid,
                    phase=phase,
                    attempt=last_attempt,
                    category=last_cat,
                    ic_mean=float(m["ic_mean"]),
                    ic_positive_ratio=float(m["ic_positive_ratio"]),
                    t=float(last_t),
                    ir=float(m["ir"]),
                    action="abandon",
                )
            )
            n_abandon += 1
            continue

        attempt_name, metrics, t = won
        action = "would_apply"
        if apply:
            apply_repair(aid, attempt_name, orig_ic, orig_cat, metrics)
            # Reload module to confirm wrapper still alive.
            reg = _fresh_registry()
            confirm = reg.compute(aid, panel)
            cat2, metrics2, t2 = _evaluate(confirm, return_df)
            if cat2 != "alive":
                raise RuntimeError(
                    f"{aid}: applied {attempt_name} but re-eval → {cat2} "
                    f"(ic={metrics2['ic_mean']})"
                )
            metrics, t = metrics2, t2
            action = "applied"
        results.append(
            EvalRow(
                id=aid,
                phase=phase,
                attempt=attempt_name,
                category="alive",
                ic_mean=float(metrics["ic_mean"]),
                ic_positive_ratio=float(metrics["ic_positive_ratio"]),
                t=float(t),
                ir=float(metrics["ir"]),
                action=action,
            )
        )
        n_alive += 1
        print(f"  OK  {aid}: {action} via {attempt_name}")

    print(
        f"\nPhase {phase}: candidates={len(cands)} "
        f"success={n_alive} abandon={n_abandon} apply={apply}"
    )
    return results


def _merge_log(new_rows: list[EvalRow]) -> dict[str, Any]:
    existing: dict[str, Any] = {"results": []}
    if LOG_PATH.is_file():
        try:
            existing = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {"results": []}
    prev = existing.get("results") or []
    # Replace same id+phase entries.
    keys = {(r.id, r.phase) for r in new_rows}
    kept = [
        r
        for r in prev
        if (r.get("id"), r.get("phase")) not in keys
    ]
    kept.extend(asdict(r) for r in new_rows)
    payload = {
        "universe": UNIVERSE,
        "period": PERIOD,
        "results": kept,
        "summary": {
            "applied": sum(1 for r in kept if r.get("action") == "applied"),
            "would_apply": sum(1 for r in kept if r.get("action") == "would_apply"),
            "abandon": sum(1 for r in kept if r.get("action") == "abandon"),
            "alive_rows": sum(1 for r in kept if r.get("category") == "alive"),
        },
    }
    LOG_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("reversed", "near_miss", "dead", "all"),
        default="reversed",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write successful repairs into gtja191 alpha_*.py files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process first N candidates (debug)",
    )
    args = parser.parse_args(argv)

    phases = (
        ["reversed", "near_miss", "dead"]
        if args.phase == "all"
        else [args.phase]
    )
    all_rows: list[EvalRow] = []
    for phase in phases:
        print(f"\n======== PHASE {phase} ========")
        all_rows.extend(
            repair_phase(phase, apply=args.apply, limit=args.limit)
        )
    payload = _merge_log(all_rows)
    print(f"\nLog written: {LOG_PATH}")
    print("summary:", json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
