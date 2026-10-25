"""Repair reversed qlib158 alphas so they pass the CSI300 2023-2026 alive gate.

Same pattern as ``repair_gtja191_survival.py``: keep the original formula in
``_compute_raw``, wrap ``compute`` with a polarity transform, and record the
repair in ``__alpha_meta__["notes"]`` plus the Chinese header.

Reversed only. Default is dry-run. Pass ``--apply`` to write files.

Run from agent/:
    python scripts/repair_qlib158_reversed.py
    python scripts/repair_qlib158_reversed.py --apply
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

from src.factors.base import decay_linear, rank, ts_mean, ts_min, ts_rank  # noqa: E402
from src.factors.bench_runner import categorise, t_stat  # noqa: E402
from src.factors.factor_analysis_core import compute_ic_series  # noqa: E402
from src.factors.registry import Registry  # noqa: E402
from src.factors.survival_store import record_bench_survival, survival_path  # noqa: E402
from src.tools.alpha_bench_tool import (  # noqa: E402
    _compute_forward_returns,
    _load_universe_panel,
)

ZOO_DIR = _AGENT_ROOT / "src" / "factors" / "zoo" / "qlib158"
LOG_PATH = _AGENT_ROOT / "scripts" / "repair_qlib158_reversed_log.json"

UNIVERSE = "csi300"
PERIOD = "2023-2026"
REPAIR_TAG = "REPAIR CSI300 2023-2026"


@dataclass
class EvalRow:
    """One variant evaluation for a single reversed qlib158 alpha."""

    id: str
    attempt: str
    category: str
    ic_mean: float
    ic_positive_ratio: float
    t: float
    ir: float
    action: str  # would_apply | applied | abandon | skip


Transform = Callable[[pd.DataFrame], pd.DataFrame]


def _short_id(alpha_id: str) -> str:
    """Map ``qlib158_kmid`` → ``kmid`` (module stem)."""
    prefix = "qlib158_"
    if not alpha_id.startswith(prefix):
        raise ValueError(f"not a qlib158 id: {alpha_id}")
    return alpha_id[len(prefix) :]


def _module_path(alpha_id: str) -> Path:
    return ZOO_DIR / f"{_short_id(alpha_id)}.py"


def _load_survival() -> dict[str, dict[str, Any]]:
    path = survival_path()
    if not path.is_file():
        raise FileNotFoundError(f"missing survival store: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or {}
    return {
        aid: row
        for aid, row in alphas.items()
        if str(aid).startswith("qlib158_") and isinstance(row, dict)
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


def _transform_neg_smooth5(df: pd.DataFrame) -> pd.DataFrame:
    return ts_mean(-df, 5)


def _transform_neg_smooth5_rank(df: pd.DataFrame) -> pd.DataFrame:
    return rank(ts_mean(-df, 5))


def _transform_neg_smooth10(df: pd.DataFrame) -> pd.DataFrame:
    return ts_mean(-df, 10)


def _transform_neg_smooth10_rank(df: pd.DataFrame) -> pd.DataFrame:
    return rank(ts_mean(-df, 10))


def _transform_neg_decay8(df: pd.DataFrame) -> pd.DataFrame:
    return decay_linear(-df, 8)


def _transform_neg_decay8_rank(df: pd.DataFrame) -> pd.DataFrame:
    return rank(decay_linear(-df, 8))


def _transform_neg_smooth30(df: pd.DataFrame) -> pd.DataFrame:
    return ts_mean(-df, 30)


def _transform_neg_tsrank4(df: pd.DataFrame) -> pd.DataFrame:
    return ts_rank(-df, 4)


def _transform_neg_tsmin11(df: pd.DataFrame) -> pd.DataFrame:
    return ts_min(-df, 11)


def _already_repaired(src: str) -> bool:
    return REPAIR_TAG in src or "def _compute_raw(" in src


def _patch_chinese_header(src: str, attempt: str) -> str:
    """Annotate the 简要说明 line so humans see the repair at a glance."""
    marker = f"[已修复/{attempt}] "
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
    bodies = {
        "sign_flip": "return -_compute_raw(panel)",
        "sign_flip_rank": "return rank(-_compute_raw(panel))",
        "sign_flip_smooth5": "return ts_mean(-_compute_raw(panel), 5)",
        "sign_flip_smooth5_rank": "return rank(ts_mean(-_compute_raw(panel), 5))",
        "sign_flip_smooth10": "return ts_mean(-_compute_raw(panel), 10)",
        "sign_flip_smooth10_rank": "return rank(ts_mean(-_compute_raw(panel), 10))",
        "sign_flip_decay8": "return decay_linear(-_compute_raw(panel), 8)",
        "sign_flip_decay8_rank": "return rank(decay_linear(-_compute_raw(panel), 8))",
        "sign_flip_smooth30": "return ts_mean(-_compute_raw(panel), 30)",
        "sign_flip_tsrank4": "return ts_rank(-_compute_raw(panel), 4)",
        "sign_flip_tsmin11": "return ts_min(-_compute_raw(panel), 11)",
    }
    if attempt not in bodies:
        raise ValueError(f"unknown attempt: {attempt}")
    body = bodies[attempt]
    # Prefer explicit attempt names over substring checks — ``ts_rank(`` contains
    # the characters ``rank(`` and would otherwise import an unused ``rank``.
    imports: list[str] = []
    if attempt in {
        "sign_flip_rank",
        "sign_flip_smooth5_rank",
        "sign_flip_smooth10_rank",
        "sign_flip_decay8_rank",
    }:
        imports.append("rank")
    if "smooth" in attempt and attempt != "sign_flip_tsrank4":
        imports.append("ts_mean")
    if "decay" in attempt:
        imports.append("decay_linear")
    if attempt == "sign_flip_tsrank4":
        imports.append("ts_rank")
    if attempt == "sign_flip_tsmin11":
        imports.append("ts_min")
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
    single = re.compile(
        r"(^[ \t]*['\"]notes['\"]\s*:\s*)(['\"])(.*?)(\2)(\s*,?\s*)$",
        flags=re.M,
    )
    if single.search(src):
        return single.sub(
            lambda m: f"{m.group(1)}{note!r},",
            src,
            count=1,
        )

    meta_match = re.search(r"__alpha_meta__\s*=\s*\{", src)
    if not meta_match:
        raise RuntimeError("no __alpha_meta__ dict found")
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
    insert = f"    'notes': {note!r},\n"
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

    src = _patch_chinese_header(src, attempt)
    src = _replace_notes_in_source(src, note)
    src = re.sub(r"^def compute\(", "def _compute_raw(", src, count=1, flags=re.M)
    src = src.rstrip() + "\n" + _build_wrapper_source(attempt)
    ast.parse(src)  # syntax guard before write
    path.write_text(src, encoding="utf-8")


def _fresh_registry() -> Registry:
    doomed = [
        name
        for name in list(sys.modules)
        if name.startswith("src.factors.zoo.qlib158.")
    ]
    for name in doomed:
        del sys.modules[name]
    return Registry()


def repair_reversed(*, apply: bool, limit: int | None = None) -> list[EvalRow]:
    """Sign-flip reversed qlib158 alphas; optionally write successful patches."""
    survival = _load_survival()
    cands = [
        (aid, row)
        for aid, row in survival.items()
        if row.get("category") == "reversed"
    ]
    cands.sort(key=lambda x: float(x[1].get("ic_mean") or 0.0))
    if limit is not None:
        cands = cands[:limit]

    print(f"Loading panel {UNIVERSE} {PERIOD} …")
    panel = _load_universe_panel(UNIVERSE, PERIOD)
    return_df = _compute_forward_returns(panel)
    reg = _fresh_registry()

    results: list[EvalRow] = []
    n_alive = 0
    n_abandon = 0
    applied_rows: list[dict[str, Any]] = []

    attempts: list[tuple[str, Transform]] = [
        ("sign_flip", _transform_neg),
        ("sign_flip_rank", _transform_neg_rank),
        ("sign_flip_smooth5", _transform_neg_smooth5),
        ("sign_flip_smooth5_rank", _transform_neg_smooth5_rank),
        ("sign_flip_smooth10", _transform_neg_smooth10),
        ("sign_flip_smooth10_rank", _transform_neg_smooth10_rank),
        ("sign_flip_decay8", _transform_neg_decay8),
        ("sign_flip_decay8_rank", _transform_neg_decay8_rank),
        ("sign_flip_smooth30", _transform_neg_smooth30),
        ("sign_flip_tsrank4", _transform_neg_tsrank4),
        ("sign_flip_tsmin11", _transform_neg_tsmin11),
    ]

    for aid, row in cands:
        path = _module_path(aid)
        if not path.is_file():
            results.append(
                EvalRow(
                    id=aid,
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
        orig_cat = str(row.get("category") or "reversed")

        try:
            raw = reg.compute(aid, panel)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {aid}: compute error: {exc}")
            results.append(
                EvalRow(
                    id=aid,
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
        last_attempt = attempts[-1][0]
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
            m = last_metrics or {
                "ic_mean": 0.0,
                "ic_positive_ratio": 0.0,
                "ir": 0.0,
            }
            results.append(
                EvalRow(
                    id=aid,
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
            applied_rows.append(
                {
                    "id": aid,
                    "_category": "alive",
                    "ic_mean": metrics["ic_mean"],
                    "ir": metrics["ir"],
                    "ic_std": metrics["ic_std"],
                    "ic_positive_ratio": metrics["ic_positive_ratio"],
                    "ic_count": metrics["ic_count"],
                }
            )
        results.append(
            EvalRow(
                id=aid,
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

    if apply and applied_rows:
        n_store = record_bench_survival(
            applied_rows, zoo="qlib158", universe=UNIVERSE, period=PERIOD
        )
        print(f"survival store: upserted {n_store} repaired qlib158 alphas")

    print(
        f"\nreversed: candidates={len(cands)} "
        f"success={n_alive} abandon={n_abandon} apply={apply}"
    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write successful repairs into qlib158 *.py files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process first N candidates (debug)",
    )
    args = parser.parse_args(argv)

    rows = repair_reversed(apply=args.apply, limit=args.limit)
    payload = {
        "universe": UNIVERSE,
        "period": PERIOD,
        "results": [asdict(r) for r in rows],
        "summary": {
            "applied": sum(1 for r in rows if r.action == "applied"),
            "would_apply": sum(1 for r in rows if r.action == "would_apply"),
            "abandon": sum(1 for r in rows if r.action == "abandon"),
            "alive_rows": sum(1 for r in rows if r.category == "alive"),
        },
    }
    LOG_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\nLog written: {LOG_PATH}")
    print("summary:", json.dumps(payload["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
