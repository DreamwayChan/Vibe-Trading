"""Entry templates for an anomaly and a behavioral bias.

Each template pre-fills the five hypothesis fields. The sentences name a
mechanism to falsify. They do not report a return, a Sharpe, or a rank from
a scan. Filling one in does not admit a 「最优」 catalog run.
"""

from __future__ import annotations

from typing import Any

# English and Chinese copies are the strings the journal form fills in.
# Other locales keep their own wording and must still pass the five-field gate.

_ANOMALY_EN = {
    "observation": (
        "On the recorded session the name's close printed at the limit-up price, "
        "and the next session is a later date."
    ),
    "hypothesis": (
        "Because shares bought into a limit-up cannot be sold until the next session, "
        "therefore the next open meets that queued supply."
    ),
    "logic": (
        "The queue is a trading-rule constraint on the offer side. "
        "It is a market-structure claim, not a measured return."
    ),
    "validation_plan": (
        "Daily lake bars for one named symbol, a window filled by the user, "
        "the next open versus the limit-up close, with A-share commission, stamp, "
        "slippage, and T+1. This sentence states the check. It is not a result."
    ),
    "invalidation": (
        "The next open is not below the limit-up close, or the cost-aware validation gate fails."
    ),
    "thesis": "Limit-up aftermath is a queued-supply claim until the cost-aware gate passes.",
    "planned_position": "Write the planned weight. This template does not size a position.",
    "planned_risk": "Write the planned loss. This template does not set a stop.",
}

_ANOMALY_ZH = {
    "observation": "记录日该标的收在涨停价，下一交易日是更晚的日期。",
    "hypothesis": "因为涨停价买入的股份要到下一交易日才能卖出，因此下一开盘面对这笔排队的供给。",
    "logic": "排队来自交易规则对卖出的约束。这是市场结构，不是已测出的收益。",
    "validation_plan": (
        "数据湖日线，用户填写的标的和窗口，比较下一开盘与涨停收盘，"
        "计入佣金、印花税、滑点和 T+1。这句话只写检查方法，不是结果。"
    ),
    "invalidation": "下一开盘不低于涨停收盘，或计成本验证门不通过。",
    "thesis": "涨停次日回吐只是排队供给的说法，计成本验证门通过之前不算优势。",
    "planned_position": "写上计划仓位。模板不决定仓位。",
    "planned_risk": "写上计划亏损。模板不设定止损。",
}

_BEHAVIORAL_EN = {
    "observation": "Friday's close and the following Monday's open are two recorded prints for the same name.",
    "hypothesis": (
        "Because holders dislike bearing news they cannot trade over the weekend, "
        "therefore Friday's close is offered relative to Monday's open."
    ),
    "logic": (
        "The claim is loss aversion across a gap with no session in between. "
        "It does not say that Mondays rise by any amount."
    ),
    "validation_plan": (
        "Daily lake bars, Friday close to Monday open, A-share costs on, "
        "window filled by the user. This sentence states the check. It is not a result."
    ),
    "invalidation": (
        "The Friday-to-Monday open gap is flat or the wrong sign over the stated window, "
        "or the cost-aware validation gate fails."
    ),
    "thesis": "Weekend risk aversion is a gap-aversion claim until the cost-aware gate passes.",
    "planned_position": "Write the planned weight. This template does not size a position.",
    "planned_risk": "Write the planned loss. This template does not set a stop.",
}

_BEHAVIORAL_ZH = {
    "observation": "同一标的的周五收盘和随后周一开盘是两笔已记录的价格。",
    "hypothesis": "因为持有人不愿承担周末无法交易的消息，因此周五收盘相对周一开盘被压低。",
    "logic": "这是跨过没有交易时段的损失厌恶。它不声称周一上涨多少。",
    "validation_plan": "数据湖日线，周五收盘到周一开盘，计入 A 股成本，窗口由用户填写。这句话只写检查方法，不是结果。",
    "invalidation": "所述窗口里周五到周一开盘的缺口持平或方向相反，或计成本验证门不通过。",
    "thesis": "周末风险厌恶只是缺口厌恶的说法，计成本验证门通过之前不算优势。",
    "planned_position": "写上计划仓位。模板不决定仓位。",
    "planned_risk": "写上计划亏损。模板不设定止损。",
}


def _row(
    template_id: str,
    kind: str,
    title_en: str,
    title_zh: str,
    en: dict[str, str],
    zh: dict[str, str],
) -> dict[str, Any]:
    return {
        "id": template_id,
        "kind": kind,
        "title_en": title_en,
        "title_zh": title_zh,
        "en": dict(en),
        "zh": dict(zh),
    }


TEMPLATES: tuple[dict[str, Any], ...] = (
    _row(
        "anomaly_limit_up_aftermath",
        "anomaly",
        "Limit-up aftermath",
        "涨停次日回吐",
        _ANOMALY_EN,
        _ANOMALY_ZH,
    ),
    _row(
        "bias_weekend_risk_aversion",
        "behavioral",
        "Weekend risk aversion",
        "周末风险厌恶",
        _BEHAVIORAL_EN,
        _BEHAVIORAL_ZH,
    ),
)


def list_templates() -> list[dict[str, Any]]:
    """Copies of the entry templates. Callers cannot mutate the module rows."""
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "title_en": row["title_en"],
            "title_zh": row["title_zh"],
            "en": dict(row["en"]),
            "zh": dict(row["zh"]),
        }
        for row in TEMPLATES
    ]
