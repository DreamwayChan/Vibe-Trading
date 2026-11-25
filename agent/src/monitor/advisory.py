"""Contextual Pre-Trade AI Copilot (监控看板按需 AI 研判参谋) — Advisory Only.

CAPABILITY CIRCLE HARD RULE:
  This module NEVER mutates ``config.tradable``, NEVER alters ``ProcessMandate``
  caps, NEVER writes orders, and NEVER auto-approves journal entries.

Instead, when the user clicks "AI 结合项目研判" on one of the 2-3 focus picks on
``/monitor``, this module packages all 6 layers of deterministic project context
(market breadth, sector rank, bound strategy alpha/cost-tax/soak, conviction
scores, personal portfolio slots, and ATR stop/entry bands), runs a structured
Devil's-Advocate review (via configured LLM, with a zero-key deterministic
fallback), and produces a ready-to-edit 5-field ``DecisionJournal`` draft.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from src.monitor.conviction import ConvictionItem, ConvictionSummary, build_conviction_summary
from src.monitor.models import MonitorConfig
from src.monitor.my_portfolio import MyPortfolio, load_my_portfolio, normalize_ashare_symbol
from src.monitor.trade_plan import TradePlanSheet, build_trade_plan

logger = logging.getLogger(__name__)

Verdict = Literal["execute_plan", "probe_small", "wait_skip", "exit_discipline"]

BOUNDARY_NOTE = (
    "仅作交易前交叉质询参考：AI 不修改可交易池准入（tradable）、"
    "不突破 Process 仓位上限、不自动发单；最终拍板与挂单由你本人执行。"
)


@dataclass
class AdvisoryReport:
    """Structured pre-trade review + pre-filled 5-field journal draft."""

    symbol: str
    as_of: str
    generated_at: str
    mode: Literal["llm", "rule_fallback"]
    advisory_only: bool = True
    verdict: Verdict = "wait_skip"
    verdict_label: str = "建议观望等待"
    confidence_note: str = ""
    supporting_points: list[str] = field(default_factory=list)
    devil_advocate_risks: list[str] = field(default_factory=list)
    tomorrow_playbook: list[str] = field(default_factory=list)
    narrative: str = ""
    context_digest: dict[str, Any] = field(default_factory=dict)
    journal_draft: dict[str, Any] = field(default_factory=dict)
    boundary_note: str = BOUNDARY_NOTE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assemble_symbol_context(
    config: MonitorConfig,
    symbol: str,
    *,
    as_of: str | None = None,
    portfolio: MyPortfolio | None = None,
    runs_root: Path | None = None,
) -> dict[str, Any]:
    """Gather all deterministic project evidence for ``symbol`` without mutating ``config``."""
    sym = normalize_ashare_symbol(symbol)
    scan_day = as_of or _infer_as_of(config)
    book = portfolio if portfolio is not None else load_my_portfolio()

    conv_dict = config.conviction
    conv_summary = (
        ConvictionSummary.from_dict(conv_dict)
        if isinstance(conv_dict, Mapping)
        else None
    )
    if conv_summary is None:
        conv_summary = build_conviction_summary(
            config, as_of=scan_day, max_focus=book.max_positions
        )

    plan_dict = config.trade_plan
    exec_day = _infer_exec_day(config, scan_day)
    if not isinstance(plan_dict, Mapping):
        plan_sheet: TradePlanSheet | None = build_trade_plan(
            config,
            as_of=scan_day,
            exec_session=exec_day,
            conviction=conv_summary,
            portfolio=book,
        )
        plan_dict = plan_sheet.to_dict()

    conv_item: dict[str, Any] | None = None
    for item in conv_summary.items:
        if item.symbol == sym:
            conv_item = item.to_dict()
            break

    plan_row: dict[str, Any] | None = None
    for group_key in ("position_actions", "entry_plans"):
        for row in (plan_dict.get(group_key) or []) if isinstance(plan_dict, Mapping) else []:
            if isinstance(row, Mapping) and str(row.get("symbol") or "").upper() == sym:
                plan_row = dict(row)
                break

    holding_row = next(
        (h.to_dict() for h in config.holdings if h.symbol.upper() == sym), None
    )
    suggestion_row = next(
        (s.to_dict() for s in config.suggestions if s.symbol.upper() == sym), None
    )
    real_pos = next(
        (p.to_dict() for p in book.positions if p.symbol.upper() == sym), None
    )

    run_metrics = _load_bound_run_summary(config, runs_root=runs_root)
    paper_soak = _load_paper_soak_summary(config)

    return {
        "symbol": sym,
        "as_of": scan_day,
        "exec_session": exec_day,
        "board_tradable": bool(config.tradable),
        "board_tradable_reasons": list(config.tradable_reasons),
        "decision": config.decision.to_dict() if config.decision is not None else None,
        "breadth": conv_summary.breadth,
        "conviction_headline": conv_summary.headline,
        "conviction_item": conv_item,
        "trade_plan_row": plan_row,
        "holding": holding_row,
        "suggestion": suggestion_row,
        "my_portfolio": {
            "total_capital": book.total_capital,
            "effective_cash": book.effective_cash(),
            "max_positions": book.max_positions,
            "held_count": len(book.positions),
            "max_stop_loss_pct": book.max_stop_loss_pct,
            "risk_per_trade_pct": book.risk_per_trade_pct,
            "current_position_in_symbol": real_pos,
        },
        "strategy_evidence": run_metrics,
        "paper_book_soak": paper_soak,
    }


def generate_symbol_advisory(
    config: MonitorConfig,
    symbol: str,
    *,
    as_of: str | None = None,
    user_note: str = "",
    use_llm: bool = True,
    portfolio: MyPortfolio | None = None,
    runs_root: Path | None = None,
    llm_invoker: Callable[[str, str], str] | None = None,
) -> AdvisoryReport:
    """Produce a pre-trade advisory report without mutating board state."""
    context = assemble_symbol_context(
        config,
        symbol,
        as_of=as_of,
        portfolio=portfolio,
        runs_root=runs_root,
    )
    sym = context["symbol"]
    scan_day = context["as_of"]
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    baseline = _build_rule_advisory(context, now_iso=now_iso, user_note=user_note)
    if not use_llm:
        return baseline

    invoke_fn = llm_invoker or _default_llm_invoke
    system_prompt, user_prompt = _build_prompts(context, user_note=user_note)
    try:
        raw_reply = invoke_fn(system_prompt, user_prompt)
        parsed = _parse_llm_json(raw_reply)
        if parsed is not None:
            return _merge_llm_into_report(baseline, parsed)
    except Exception as exc:  # noqa: BLE001 — deterministic fallback always succeeds
        logger.info("monitor advisory falling back to deterministic review: %s", exc)
    return baseline


def _build_rule_advisory(
    ctx: Mapping[str, Any],
    *,
    now_iso: str,
    user_note: str = "",
) -> AdvisoryReport:
    sym = str(ctx.get("symbol") or "")
    as_of = str(ctx.get("as_of") or "")
    exec_session = str(ctx.get("exec_session") or "")
    conv = ctx.get("conviction_item") if isinstance(ctx.get("conviction_item"), Mapping) else {}
    plan = ctx.get("trade_plan_row") if isinstance(ctx.get("trade_plan_row"), Mapping) else {}
    strat = ctx.get("strategy_evidence") if isinstance(ctx.get("strategy_evidence"), Mapping) else {}
    soak = ctx.get("paper_book_soak") if isinstance(ctx.get("paper_book_soak"), Mapping) else {}
    breadth = ctx.get("breadth") if isinstance(ctx.get("breadth"), Mapping) else {}
    my_port = ctx.get("my_portfolio") if isinstance(ctx.get("my_portfolio"), Mapping) else {}

    stars = float(conv.get("stars") or 0.0)
    vetoed = bool(conv.get("vetoed", False))
    plan_action = str(plan.get("action") or "")

    pros: list[str] = list(conv.get("evidence") or [])
    risks: list[str] = list(conv.get("cautions") or [])

    if strat.get("alpha") is not None:
        pros.append(
            f"策略历史年化 Alpha {float(strat['alpha']) * 100:+.1f}%，夏普 {float(strat.get('sharpe') or 0.0):.2f}"
        )
    if strat.get("cost_tax_return") is not None:
        tax = float(strat["cost_tax_return"])
        if tax < -0.05:
            risks.append(f"历史回测交易成本税吃掉了 {abs(tax) * 100:.1f}% 收益，切忌盘中频繁换手")

    if soak.get("decayed"):
        risks.append("20日纸面浸泡显示近期夏普出现衰减，实盘宜减半仓位或观望")
    elif soak.get("session_count", 0) >= 5:
        pros.append(f"纸面账本已跟踪 {soak['session_count']} 个交易日，未触发超阈偏离")

    if not ctx.get("board_tradable"):
        reasons = "、".join(str(r) for r in (ctx.get("board_tradable_reasons") or [])[:3])
        risks.append(f"当前看板尚未进入正式可交易池（原因：{reasons or '待补齐五栏日志/验证门'}）")

    if not risks:
        risks.append("A 股次日集合竞价若出现板块集体大幅高开（>3.5%），容易演变为兑现砸盘日")

    # Determine verdict
    if plan_action in ("stop_exit", "signal_exit"):
        verdict: Verdict = "exit_discipline"
        verdict_label = "优先执行离场/止损纪律"
    elif vetoed or stars < 2.8:
        verdict = "wait_skip"
        verdict_label = "建议放弃 · 空仓/观望休息"
    elif stars >= 3.8 and plan_action == "buy_focus" and ctx.get("board_tradable"):
        verdict = "execute_plan"
        verdict_label = "共振充分 · 可按计划限价定仓执行"
    elif stars >= 3.2 and plan_action in ("buy_focus", "hold_keep"):
        verdict = "probe_small"
        verdict_label = "条件达标 · 建议不追高分批/轻仓执行"
    else:
        verdict = "wait_skip"
        verdict_label = "确定性一般 · 宁缺毋滥继续观察"

    # Build concrete tomorrow playbook
    playbook: list[str] = []
    if plan_action in ("stop_exit", "signal_exit"):
        playbook.append(str(plan.get("broker_tip") or "次日早盘按纪律执行卖出，回收现金。"))
    elif plan.get("ideal_buy_low") and plan.get("stop_price"):
        low_px = float(plan["ideal_buy_low"])
        high_px = float(plan["ideal_buy_high"])
        chase_px = float(plan["chase_stop_above"])
        stop_px = float(plan["stop_price"])
        shares = int(plan.get("planned_shares") or 0)
        max_loss = float(plan.get("max_loss_rmb") or 0.0)
        playbook.append(
            f"【平开/回踩剧本】若 {exec_session} 开盘及早盘运行在 ¥{low_px:.2f}~¥{high_px:.2f} 且承接稳定，按计划挂限价买 {shares} 股。"
        )
        playbook.append(
            f"【高开放弃剧本】若集合竞价或早盘冲高超过 ¥{chase_px:.2f}，盈亏比恶化，直接放弃不追高。"
        )
        playbook.append(
            f"【认错底线剧本】买入成交后设置券商止损条件单 ¥{stop_px:.2f}（单笔封顶亏损 ¥{max_loss:,.0f}），跌破无条件离场。"
        )
    else:
        playbook.append("次日保持观察，不挂新开仓买单，等待回踩均线或大盘赚钱效应回暖。")

    note_suffix = f"（已结合你的关注点：{user_note.strip()}）" if user_note.strip() else ""
    narrative = (
        f"{sym} 当前五维共振得分 {stars:.1f}/5.0 星。{verdict_label}{note_suffix}。"
        f"核心依据：{pros[0] if pros else '暂无强共振'}；"
        f"首要防范：{risks[0] if risks else '注意高开兑现风险'}。"
    )

    journal_draft = _build_journal_draft(ctx, stars=stars, pros=pros, risks=risks)
    return AdvisoryReport(
        symbol=sym,
        as_of=as_of,
        generated_at=now_iso,
        mode="rule_fallback",
        advisory_only=True,
        verdict=verdict,
        verdict_label=verdict_label,
        confidence_note=f"五维共振 {stars:.1f}/5.0 星 ｜ 账户持仓槽位 {my_port.get('held_count', 0)}/{my_port.get('max_positions', 3)}",
        supporting_points=pros[:4],
        devil_advocate_risks=risks[:4],
        tomorrow_playbook=playbook,
        narrative=narrative,
        context_digest={
            "stars": stars,
            "breadth_summary": breadth.get("summary") if isinstance(breadth, Mapping) else None,
            "board_name": conv.get("board_name"),
            "board_rank": conv.get("board_rank"),
            "planned_shares": plan.get("planned_shares"),
            "stop_price": plan.get("stop_price"),
            "max_loss_rmb": plan.get("max_loss_rmb"),
        },
        journal_draft=journal_draft,
    )


def _build_journal_draft(
    ctx: Mapping[str, Any],
    *,
    stars: float,
    pros: list[str],
    risks: list[str],
) -> dict[str, Any]:
    """Pre-fill the 5 required causal hypothesis fields so the user can review & save in 10s."""
    sym = str(ctx.get("symbol") or "")
    as_of = str(ctx.get("as_of") or "")
    conv = ctx.get("conviction_item") if isinstance(ctx.get("conviction_item"), Mapping) else {}
    plan = ctx.get("trade_plan_row") if isinstance(ctx.get("trade_plan_row"), Mapping) else {}
    breadth = ctx.get("breadth") if isinstance(ctx.get("breadth"), Mapping) else {}

    board_txt = (
        f"所属板块【{conv.get('board_name')}】排第 {conv.get('board_rank')} 名"
        if conv.get("board_name")
        else "策略截面因子排名前列"
    )
    b_txt = str(breadth.get("summary") or "大盘环境非空头")
    stop_px = plan.get("stop_price")
    shares = int(plan.get("planned_shares") or 0)
    cap_w = float(plan.get("capital_weight") or 0.0)
    max_loss = float(plan.get("max_loss_rmb") or 0.0)
    max_loss_pct = float(plan.get("max_loss_capital_pct") or 0.015)

    invalidation_txt = (
        f"price < {float(stop_px):.2f}（日线跌破结构/ATR硬止损位 ¥{float(stop_px):.2f}，或策略信号转空离场）"
        if stop_px
        else "日线收盘跌破近10日低点或亏损达 10% 硬止损线"
    )
    return {
        "symbol": sym,
        "observation": f"截至 {as_of}，{sym} {board_txt}，五维共振 {stars:.1f}/5 星；{b_txt}",
        "hypothesis": (
            f"因为当前市场宽度允许承担结构性风险且 {sym} 处于主线顺风与均线舒适买点，"
            "因此在控制追高前提下具备正期望波段延续性。"
        ),
        "logic": "；".join(pros[:3]) or "大盘气候 + 板块顺风 + 均线结构回踩共振",
        "validation_plan": (
            "以绑定策略 A 股真实费率（佣金万2.5/印花税万5/滑点千1/T+1）回测验证门及 20 日纸面账本跟踪偏离度检验。"
        ),
        "invalidation": invalidation_txt,
        "thesis": f"{sym} 波段顺风低吸计划（共振 {stars:.1f} 星）",
        "planned_position": (
            f"{shares} 股（约占总本金 {cap_w * 100:.1f}%）" if shares > 0 else "观察仓 / 暂不开仓"
        ),
        "planned_risk": (
            f"单笔最大止损 ¥{max_loss:,.0f}（占总本金 {max_loss_pct * 100:.2f}%，个股止损线 ¥{float(stop_px):.2f}）"
            if stop_px and shares > 0
            else f"个股止损不超 -10%，单笔本金风险不超 1.5%（留意：{risks[0] if risks else '高开不追'}）"
        ),
        "invalidation_price": float(stop_px) if stop_px else None,
        "invalidation_side": "below" if stop_px else None,
    }


def _build_prompts(ctx: Mapping[str, Any], *, user_note: str) -> tuple[str, str]:
    system = (
        "你是个人 A 股波段投资者的「交易前风控陪审官（Pre-Trade Devil's Advocate）」。"
        "用户的真实约束是：资金有限、时间精力有限、最多同时拿 2~3 只票、只做日线波段多头、单票最大止损 -10%。\n"
        "你的职责不是吹票，而是基于系统提供的确定性项目数据，帮用户做最后一道把关，给出冷静、客观、有底气的判断。\n"
        "严格只输出合法 JSON 对象，字段如下：\n"
        '{\n'
        '  "verdict": "execute_plan" | "probe_small" | "wait_skip" | "exit_discipline",\n'
        '  "verdict_label": "12字以内的明确结论",\n'
        '  "supporting_points": ["共振支撑点1", "共振支撑点2"],\n'
        '  "devil_advocate_risks": ["反方挑刺/潜在盲点1", "反方挑刺2"],\n'
        '  "tomorrow_playbook": ["明日平开/回踩应对", "明日高开/低开应对", "止损纪律底线"],\n'
        '  "narrative": "100字以内的核心定性总结"\n'
        '}'
    )
    user_payload = {
        "project_context": ctx,
        "user_question": user_note.strip() or "请帮我评估这笔交易值不值得明天出手，最大的坑在哪？",
    }
    return system, json.dumps(user_payload, ensure_ascii=False, default=str)


def _default_llm_invoke(system_prompt: str, user_prompt: str) -> str:
    from src.providers.llm import build_llm

    llm = build_llm(temperature=0.2)
    resp = llm.invoke(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        return "".join(
            str(part.get("text") if isinstance(part, dict) else part)
            for part in content
        )
    return str(content or "")


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else cleaned
    if not candidate.startswith("{"):
        brace = re.search(r"(\{.*\})", candidate, flags=re.DOTALL)
        if not brace:
            return None
        candidate = brace.group(1)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _merge_llm_into_report(base: AdvisoryReport, llm_data: Mapping[str, Any]) -> AdvisoryReport:
    verdict_raw = str(llm_data.get("verdict") or base.verdict).strip()
    verdict: Verdict = (
        verdict_raw  # type: ignore[assignment]
        if verdict_raw in ("execute_plan", "probe_small", "wait_skip", "exit_discipline")
        else base.verdict
    )
    # Safety rail: LLM can NEVER upgrade a vetoed/exit_discipline baseline into execute_plan!
    if base.verdict == "exit_discipline":
        verdict = "exit_discipline"
    elif base.verdict == "wait_skip" and verdict == "execute_plan":
        verdict = "probe_small"

    def _str_list(key: str, fallback: list[str]) -> list[str]:
        raw = llm_data.get(key)
        if not isinstance(raw, list):
            return fallback
        items = [str(x).strip() for x in raw if str(x).strip()]
        return items[:5] if items else fallback

    return AdvisoryReport(
        symbol=base.symbol,
        as_of=base.as_of,
        generated_at=base.generated_at,
        mode="llm",
        advisory_only=True,
        verdict=verdict,
        verdict_label=str(llm_data.get("verdict_label") or base.verdict_label).strip()[:40],
        confidence_note=base.confidence_note,
        supporting_points=_str_list("supporting_points", base.supporting_points),
        devil_advocate_risks=_str_list("devil_advocate_risks", base.devil_advocate_risks),
        tomorrow_playbook=_str_list("tomorrow_playbook", base.tomorrow_playbook),
        narrative=str(llm_data.get("narrative") or base.narrative).strip(),
        context_digest=base.context_digest,
        journal_draft=base.journal_draft,
        boundary_note=BOUNDARY_NOTE,
    )


def _infer_as_of(config: MonitorConfig) -> str:
    for item in config.suggestions:
        if item.as_of:
            return item.as_of
    for h in config.holdings:
        if h.data_as_of:
            return h.data_as_of
    if config.decision and config.decision.as_of:
        return config.decision.as_of
    return datetime.now(timezone.utc).date().isoformat()


def _infer_exec_day(config: MonitorConfig, scan_day: str) -> str:
    for item in config.suggestions:
        if item.exec_as_of:
            return item.exec_as_of
    from datetime import date
    from src.monitor.scan import _next_session_offline

    try:
        return _next_session_offline(date.fromisoformat(scan_day[:10]))
    except ValueError:
        return scan_day


def _load_bound_run_summary(
    config: MonitorConfig,
    *,
    runs_root: Path | None = None,
) -> dict[str, Any]:
    run_id = str(getattr(config.strategy, "run_id", "") or "").strip()
    if not run_id:
        return {}
    try:
        from src.api.helpers import RUNS_DIR
        from src.monitor.store import is_safe_history_key

        if not is_safe_history_key(run_id):
            return {}
        root = (runs_root or RUNS_DIR).resolve()
        run_dir = (root / run_id).resolve()
        run_dir.relative_to(root)
        if not run_dir.is_dir():
            return {}
        out: dict[str, Any] = {"run_id": run_id}
        card_path = run_dir / "artifacts" / "run_card.json"
        if card_path.is_file():
            card = json.loads(card_path.read_text(encoding="utf-8"))
            metrics = card.get("metrics") if isinstance(card, Mapping) else {}
            if isinstance(metrics, Mapping):
                for key in ("total_return", "sharpe_ratio", "max_drawdown", "win_rate", "alpha", "beta"):
                    if key in metrics:
                        out[key.replace("sharpe_ratio", "sharpe")] = metrics[key]
        cc_path = run_dir / "artifacts" / "cost_compare.json"
        if cc_path.is_file():
            cc = json.loads(cc_path.read_text(encoding="utf-8"))
            delta = (cc.get("metrics") or {}).get("delta") if isinstance(cc, Mapping) else {}
            if isinstance(delta, Mapping) and "total_return" in delta:
                out["cost_tax_return"] = delta["total_return"]
        return out
    except Exception:  # noqa: BLE001
        return {}


def _load_paper_soak_summary(config: MonitorConfig) -> dict[str, Any]:
    run_id = str(getattr(config.strategy, "run_id", "") or "").strip()
    if not run_id:
        return {}
    try:
        from src.paper_book.store import load_window, paper_book_root

        root = paper_book_root()
        if not (root / run_id / "reports" / "window.json").is_file():
            return {}
        win = load_window(root, run_id)
        if not isinstance(win, Mapping):
            return {}
        decay = win.get("decay") if isinstance(win.get("decay"), Mapping) else {}
        return {
            "session_count": win.get("session_count"),
            "cum_return": win.get("cum_return"),
            "return_deviation": win.get("return_deviation"),
            "downgraded": win.get("downgraded"),
            "decayed": decay.get("decayed"),
        }
    except Exception:  # noqa: BLE001
        return {}
