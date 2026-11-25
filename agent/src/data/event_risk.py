"""Deterministic A-share event & calendar risk filter (事件排雷护栏).

Swing positions held over 5-20 sessions are vulnerable to scheduled landmines
that pure OHLCV factors cannot see:
  1. Imminent earnings disclosure (预约披露日 <= 3 trading days -> veto new buy);
  2. Large lock-up share unlock (5 trading days before unlock >= 5% float -> veto new buy);
  3. Statutory danger window (late-April annual + Q1 report cluster -> caution);
  4. Extreme intraday amplitude / volume blow-off at highs (高位放量巨振预警).

All checks run 100% offline against optional local event files/lake tables plus
the bars already loaded in memory.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def event_calendar_path() -> Path:
    return Path.home() / ".vibe-trading" / "lake" / "events" / "calendar.json"


def load_local_events() -> list[dict[str, Any]]:
    """Read locally cached earnings / unlock events; empty list when absent."""
    path = event_calendar_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("event calendar unreadable: %s", exc)
        return []
    if isinstance(raw, dict):
        items = raw.get("events") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    return [row for row in items if isinstance(row, dict)]


def save_local_events(events: Sequence[Mapping[str, Any]]) -> Path:
    path = event_calendar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"events": [dict(item) for item in events]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def assess_event_risks(
    symbols: Sequence[str],
    *,
    as_of: str,
    data_map: Mapping[str, pd.DataFrame] | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Return ``symbol -> [flag, ...]`` where ``veto:...`` blocks new focus buys."""
    as_of_date = dt.date.fromisoformat(str(as_of)[:10])
    rows = list(events) if events is not None else load_local_events()

    by_sym: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            by_sym.setdefault(sym, []).append(row)

    # Late-April (Apr 20 - Apr 30) and late-January (Jan 22 - Jan 31) are the
    # classic A-share earnings-shock clusters.
    in_april_cluster = as_of_date.month == 4 and as_of_date.day >= 20
    in_jan_cluster = as_of_date.month == 1 and as_of_date.day >= 22

    out: dict[str, list[str]] = {}
    for raw_sym in symbols:
        sym = str(raw_sym or "").strip().upper()
        if not sym:
            continue
        flags: list[str] = []

        for ev in by_sym.get(sym, []):
            kind = str(ev.get("type") or ev.get("kind") or "").strip().lower()
            ev_date_str = str(ev.get("date") or "").strip()[:10]
            try:
                ev_date = dt.date.fromisoformat(ev_date_str)
            except ValueError:
                continue
            days_ahead = (ev_date - as_of_date).days
            if days_ahead < 0:
                continue
            if kind in ("earnings", "disclosure", "report") and days_ahead <= 5:
                if days_ahead <= 3:
                    flags.append(f"veto:财报披露前{days_ahead}日({ev_date_str})")
                else:
                    flags.append(f"临近财报预约披露({ev_date_str})")
            elif kind in ("unlock", "lockup") and days_ahead <= 7:
                ratio = _opt_float(ev.get("float_ratio") or ev.get("ratio"))
                if ratio is None or ratio >= 0.05:
                    pct_txt = f"{ratio * 100:.1f}%" if ratio is not None else "大额"
                    flags.append(f"veto:限售股解禁({ev_date_str}, {pct_txt})")
                else:
                    flags.append(f"小额限售解禁({ev_date_str})")
            elif kind in ("reduction", "insider_sell") and days_ahead <= 10:
                flags.append(f"股东减持计划窗口({ev_date_str})")

        if in_april_cluster:
            flags.append("4月下旬年报/一季报密集披露期，严控绩差题材仓位")
        elif in_jan_cluster:
            flags.append("1月下旬年度业绩预告密集窗口，防范商誉/预亏暴雷")

        frame = (data_map or {}).get(sym)
        bar_warn = _abnormal_bar_warning(frame, as_of=as_of)
        if bar_warn:
            flags.append(bar_warn)

        if flags:
            out[sym] = flags
    return out


def _abnormal_bar_warning(frame: pd.DataFrame | None, *, as_of: str) -> str | None:
    """Detect high-altitude volume blow-off / wide-range distribution bar."""
    if frame is None or getattr(frame, "empty", True):
        return None
    for col in ("high", "low", "close", "open"):
        if col not in frame.columns:
            return None
    cutoff = pd.Timestamp(as_of).normalize()
    idx = pd.to_datetime(frame.index, errors="coerce").normalize()
    sub = frame.loc[idx <= cutoff]
    if len(sub) < 15:
        return None
    last = sub.iloc[-1]
    try:
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        c = float(last["close"])
    except (TypeError, ValueError):
        return None
    if min(o, h, l, c) <= 0:
        return None
    amp = (h - l) / l
    upper_wick = (h - max(o, c)) / c
    if amp >= 0.11 and c < o and upper_wick >= 0.04:
        return f"当日高位宽幅放量收阴（振幅 {amp * 100:.1f}%），警惕短线派发分歧"
    return None


def _opt_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if np.isfinite(num) else None
