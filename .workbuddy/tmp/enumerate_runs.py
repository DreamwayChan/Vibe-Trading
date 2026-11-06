"""Enumerate monitor-picker runs (strategy_ready) with metadata for journaling."""
import csv
import json
import sys
from pathlib import Path

RUNS_DIR = Path(r"E:\Vibe-Trading\agent\runs")

out = []
for d in sorted([p for p in RUNS_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")], key=lambda x: x.name, reverse=True):
    se = d / "code" / "signal_engine.py"
    if not se.is_file():
        continue
    info = {"run_id": d.name, "status": None, "title": None, "prompt": None}
    state = d / "state.json"
    if state.is_file():
        try:
            info["status"] = json.loads(state.read_text(encoding="utf-8")).get("status")
        except Exception:
            pass
    req = d / "req.json"
    if req.is_file():
        try:
            rd = json.loads(req.read_text(encoding="utf-8"))
            info["prompt"] = rd.get("prompt")
            info["title"] = (rd.get("title") or "").strip() or None
        except Exception:
            pass
    metrics = d / "artifacts" / "metrics.csv"
    if metrics.is_file():
        try:
            with open(metrics, encoding="utf-8") as f:
                row = next(csv.DictReader(f), None)
            if row:
                info["metrics"] = {k: row.get(k) for k in ("total_return", "excess_return", "sharpe", "max_drawdown", "win_rate", "num_trades") if row.get(k) not in (None, "")}
        except Exception:
            pass
    cfg = d / "config.json"
    if cfg.is_file():
        try:
            cj = json.loads(cfg.read_text(encoding="utf-8"))
            info["config"] = {
                "start_date": cj.get("start_date"),
                "end_date": cj.get("end_date"),
                "frequency": cj.get("frequency"),
                "benchmark": cj.get("benchmark"),
                "universe_size": len(cj.get("symbols") or []) if isinstance(cj.get("symbols"), list) else None,
                "universe_hint": (cj.get("universe") or "")[:80] if isinstance(cj.get("universe"), str) else None,
            }
            syms = cj.get("symbols")
            if isinstance(syms, list) and syms:
                info["sample_symbols"] = syms[:8]
        except Exception:
            pass
    # strategy hint from signal_engine docstring
    try:
        head = se.read_text(encoding="utf-8", errors="ignore")[:3000]
        import re
        m = re.search(r'"""(.*?)"""', head, re.S)
        if m:
            info["engine_doc"] = " ".join(m.group(1).split())[:240]
    except Exception:
        pass
    out.append(info)

print(json.dumps(out, ensure_ascii=False, indent=1))
sys.stderr.write(f"count={len(out)}\n")
