"""Post-close 16:30 daily routine (一键盘后复盘流水线).

Chains the personal trading workflow into one deterministic pass:
  1. Optional market-pick scan (or reuse today's fresh snapshot);
  2. Monitor scan on the active strategy board (computing breadth, 5D conviction
     Top 2-3 focus picks, event risk checks, and personal next-day trade plan);
  3. Paper-book daily settlement for ``as_of`` (filling yesterday's causal signals);
  4. Concise Markdown digest suitable for the Monitor UI or IM push.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any

from src.monitor.models import MonitorConfig
from src.monitor.scan import run_monitor_scan
from src.monitor.store import load_monitor, save_monitor

logger = logging.getLogger(__name__)


def run_daily_close_routine(
    *,
    as_of: dt.date | str | None = None,
    config: MonitorConfig | None = None,
    run_market_pick: bool = False,
    settle_paper_book: bool = True,
    runs_root: Path | None = None,
    paper_root: Path | str | None = None,
) -> dict[str, Any]:
    """Execute the post-close pipeline and return a structured summary + digest."""
    target_date = (
        dt.date.fromisoformat(as_of[:10])
        if isinstance(as_of, str) and as_of.strip()
        else (as_of if isinstance(as_of, dt.date) else dt.date.today())
    )
    as_of_iso = target_date.isoformat()
    steps: list[dict[str, Any]] = []

    if run_market_pick:
        try:
            from src.market_pick.scan import run_market_pick_scan
            from src.market_pick.store import save_day_snapshot as save_mp_snapshot

            mp_res = run_market_pick_scan(as_of=target_date)
            save_mp_snapshot(mp_res)
            steps.append(
                {
                    "step": "market_pick",
                    "ok": True,
                    "stance": mp_res.stance,
                    "recommended_bk": mp_res.recommended_bk,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily routine market_pick skipped: %s", exc)
            steps.append({"step": "market_pick", "ok": False, "error": str(exc)})

    board = config if config is not None else load_monitor()
    updated = run_monitor_scan(board, as_of=target_date, runs_root=runs_root)
    if config is None:
        save_monitor(updated)
    steps.append(
        {
            "step": "monitor_scan",
            "ok": True,
            "provisional": any(s.provisional for s in updated.suggestions),
            "focus_symbols": (updated.conviction or {}).get("focus_symbols", []),
        }
    )

    paper_summary: dict[str, Any] | None = None
    is_provisional = any(s.provisional for s in updated.suggestions) or any(
        h.provisional for h in updated.holdings
    )
    run_id = str(getattr(updated.strategy, "run_id", "") or "").strip()
    if settle_paper_book and run_id and not is_provisional:
        try:
            from src.paper_book.book import resolve_run_dir
            from src.paper_book.soak import step_range

            r_dir = (runs_root / run_id) if runs_root is not None else resolve_run_dir(run_id)
            win = step_range(
                start=as_of_iso,
                end=as_of_iso,
                config=updated,
                run_dir=r_dir,
                root=paper_root,
                use_snapshots=True,
            )
            paper_summary = {
                "as_of": as_of_iso,
                "nav": win.nav,
                "fill_count": win.fill_count,
                "downgraded": win.downgraded,
            }
            steps.append({"step": "paper_book", "ok": True, **paper_summary})
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily routine paper_book step skipped: %s", exc)
            steps.append({"step": "paper_book", "ok": False, "error": str(exc)})

    digest = render_daily_digest(updated, as_of=as_of_iso, paper_summary=paper_summary)
    return {
        "as_of": as_of_iso,
        "steps": steps,
        "conviction": updated.conviction,
        "trade_plan": updated.trade_plan,
        "paper_book": paper_summary,
        "digest_markdown": digest,
    }


def render_daily_digest(
    config: MonitorConfig,
    *,
    as_of: str,
    paper_summary: dict[str, Any] | None = None,
) -> str:
    """Format a compact 1-minute Markdown briefing for personal review or IM push."""
    conv = config.conviction or {}
    plan = config.trade_plan or {}
    breadth = conv.get("breadth") or {}

    lines: list[str] = [f"### 📅 {as_of} 盘后复盘与次日交易计划"]
    if breadth.get("summary"):
        lines.append(f"- **市场温度计**：{breadth['summary']}")
    if conv.get("headline"):
        lines.append(f"- **共振漏斗结论**：{conv['headline']}")
    if plan.get("summary_note"):
        lines.append(f"- **账户计划摘要**：{plan['summary_note']}")

    pos_actions = plan.get("position_actions") or []
    if pos_actions:
        lines.append("\n#### 1. 现有实盘持仓处理")
        for row in pos_actions:
            lines.append(f"- **{row['symbol']}**（{row['title']}）：{row['broker_tip']}")

    entries = [r for r in (plan.get("entry_plans") or []) if r.get("action") == "buy_focus"]
    if entries:
        lines.append("\n#### 2. 次日高胜率焦点新开仓计划")
        for row in entries:
            lines.append(
                f"- **{row['symbol']}**（{row.get('stars', 0):.1f}★）：{row['broker_tip']}"
            )
    else:
        lines.append("\n#### 2. 次日新开仓计划\n- 无满足条件的新开仓买单，守住本金耐心等待。")

    if paper_summary:
        lines.append(
            f"\n> 纸面账本结算：净值 ¥{float(paper_summary.get('nav') or 0):,.2f}，"
            f"今日成交 {int(paper_summary.get('fill_count') or 0)} 笔。"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 16:30 post-close personal trading routine")
    parser.add_argument("--date", default="", help="Session date YYYY-MM-DD (default: today)")
    parser.add_argument("--with-market-pick", action="store_true", help="Also run Market Pick scan")
    parser.add_argument("--no-paper-book", action="store_true", help="Skip paper-book settlement")
    args = parser.parse_args(argv)

    result = run_daily_close_routine(
        as_of=args.date or None,
        run_market_pick=args.with_market_pick,
        settle_paper_book=not args.no_paper_book,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
