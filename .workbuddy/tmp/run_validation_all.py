"""Recompute bootstrap + walk-forward (and Monte Carlo) for every monitor run.

Uses backtest.validation.main(), i.e. the same code path BaseEngine uses when
config["validation"] is present. Nothing is synthesised: equity.csv and
trades.csv are the artifacts the backtests already produced.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, "E:/Vibe-Trading/agent")

from backtest.validation import evaluate_run_validation, main as validate_run

RUNS = Path("E:/Vibe-Trading/agent/runs")


def monitor_runs() -> list[Path]:
    return sorted(
        d for d in RUNS.iterdir() if d.is_dir() and (d / "code" / "signal_engine.py").is_file()
    )


rows = []
for run_dir in monitor_runs():
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            validate_run(run_dir)
        err = ""
    except Exception as exc:  # noqa: BLE001 — report and keep going
        err = f"{type(exc).__name__}: {exc}"

    if err:
        rows.append({"run_id": run_dir.name, "status": "ERROR", "detail": err})
        print(f"ERROR  {run_dir.name}  {err}", flush=True)
        continue

    gate = evaluate_run_validation(run_dir)
    art = json.loads((run_dir / "artifacts" / "validation.json").read_text(encoding="utf-8"))
    mc = art.get("monte_carlo") or {}
    bs = art.get("bootstrap") or {}
    wf = art.get("walk_forward") or {}
    rows.append(
        {
            "run_id": run_dir.name,
            "status": "PASS" if gate.passed else "FAIL",
            "reasons": gate.reasons,
            "p_value_sharpe": mc.get("p_value_sharpe"),
            "ci_lower": bs.get("ci_lower"),
            "consistency_rate": wf.get("consistency_rate"),
            "profit_windows": f"{wf.get('profitable_windows')}/{wf.get('n_windows')}",
            "cost_aware": art.get("cost_aware"),
        }
    )
    flag = "PASS" if gate.passed else "FAIL"
    print(
        f"{flag}  {run_dir.name[:52]:<52}  p={mc.get('p_value_sharpe')!s:<6} "
        f"ci_lo={bs.get('ci_lower')!s:<8} wf={wf.get('consistency_rate')!s:<6} "
        f"reasons={','.join(gate.reasons) or '-'}",
        flush=True,
    )

out = Path("E:/Vibe-Trading/.workbuddy/tmp/validation_summary.json")
out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
ok = sum(1 for r in rows if r.get("status") == "PASS")
print(f"\n{ok}/{len(rows)} passed")
