"""Compose one Decision from Mandate + F0 stance + structure + optional analog.

Later gates may only tighten. Analog is skipped on the daily scan unless the
caller passes calls (forecast recompose). Numeric size is a cap, not a target
to lever into.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from src.monitor.models import WEIGHT_EPSILON, HoldingRow
from src.process.mandate import DEFAULT_MANDATE, ProcessMandate
from src.process.models import (
    AnalogCall,
    Decision,
    DecisionAction,
    SymbolDecision,
    TraceStep,
)

# Structure actions that may still add (long-only dip / relative-strength hold).
_STRUCTURE_ALLOWS_ADD = frozenset({"hold", "buy_dip"})
# Knife / fade: do not add; reduce cuts existing size.
_STRUCTURE_CUTS = frozenset({"reduce"})
_ADD_ACTIONS = frozenset({"enter", "add"})


def compose(
    *,
    market_regime: str | None,
    f0_stance: str,
    holdings: Iterable[HoldingRow] | None,
    provisional: bool = False,
    analog: Mapping[str, AnalogCall] | None = None,
    mandate: ProcessMandate | None = None,
    as_of: str | None = None,
    overlays_raw: dict[str, Any] | None = None,
    previous: Decision | None = None,
    horizons: Mapping[str, Any] | None = None,
) -> Decision:
    """Run the waterfall and return a single official Decision.

    ``previous`` is the daily-scan decision. When analog recomposes, the
    result is forced not to exceed that size or reopen a wait.
    ``horizons`` is the CSI 300 short/medium/long split. Medium bear still
    blocks new opens; the book is flattened to 0% only when long is also
    bear (or horizons are missing, which keeps the old constitution).
    """
    rules = mandate or DEFAULT_MANDATE
    rows = list(holdings or [])
    regime = (market_regime or "").strip().lower() or None
    stance = (f0_stance or "none").strip().lower()
    if stance not in ("full", "half_watch", "none"):
        stance = "none"

    analog_map = {str(key).strip().upper(): value for key, value in (analog or {}).items()}
    analog_applied = bool(analog_map)
    vetoed_by: list[str] = []
    trace: list[TraceStep] = []

    allow_new, size_cap, mandate_step = _mandate_gate(
        rules, regime=regime, stance=stance, provisional=provisional, horizons=horizons
    )
    trace.append(mandate_step)
    if not mandate_step.passed:
        vetoed_by.append(mandate_step.veto or "mandate")

    allow_new, size_cap, structure_book, structure_step = _structure_gate(
        rows,
        allow_new=allow_new,
        size_cap=size_cap,
        regime=regime,
        horizons=horizons,
    )
    trace.append(structure_step)
    if structure_step.veto:
        vetoed_by.append(structure_step.veto)

    allow_new, size_cap, f0_step = _f0_gate(
        stance, allow_new=allow_new, size_cap=size_cap, mandate=rules
    )
    trace.append(f0_step)
    if f0_step.veto:
        vetoed_by.append(f0_step.veto)

    analog_blocked: set[str] = set()
    analog_step = _analog_gate(analog_map, analog_applied=analog_applied)
    trace.append(analog_step)
    if analog_applied:
        analog_blocked = {
            symbol
            for symbol, call in analog_map.items()
            if call.blocks_new_risk()
        }
        if analog_blocked and analog_step.veto:
            vetoed_by.append(analog_step.veto)

    per_symbol = _decide_symbols(
        rows,
        allow_new=allow_new,
        analog_blocked=analog_blocked,
        analog_map=analog_map,
        max_name_weight=rules.max_name_weight,
    )
    per_symbol, size_nav, size_step = _size_gate(per_symbol, size_cap=size_cap)
    trace.append(size_step)

    action = _portfolio_action(per_symbol, size_nav=size_nav)
    # A later analog pass cannot reopen a wait or grow size.
    if previous is not None:
        action, size_nav, per_symbol, tighten_note = _tighten_against_previous(
            previous, action=action, size_nav=size_nav, per_symbol=per_symbol
        )
        if tighten_note:
            trace.append(
                TraceStep(
                    gate="analog",
                    title="相对上次合成再收紧",
                    thought=tighten_note,
                    conclusion="第二次合成没有增加规模，也没有把「等待」改成买入。",
                    passed=True,
                    skipped=False,
                )
            )

    reason = _compose_reason(
        action=action,
        size_nav=size_nav,
        allow_new=allow_new,
        vetoed_by=vetoed_by,
        per_symbol=per_symbol,
        analog_applied=analog_applied,
        provisional=provisional and not rules.provisional_is_decision,
    )
    unique_veto = []
    for item in vetoed_by:
        if item not in unique_veto:
            unique_veto.append(item)

    raw = dict(overlays_raw or {})
    raw.setdefault("f0_stance", stance)
    raw.setdefault("market_regime", regime)
    raw.setdefault("provisional", provisional)
    if horizons:
        raw.setdefault("horizons", dict(horizons))

    return Decision(
        action=action,
        size_nav=round(float(size_nav), 6),
        reason=reason,
        as_of=as_of,
        mandate_hash=rules.fingerprint(),
        analog_applied=analog_applied,
        vetoed_by=unique_veto,
        per_symbol=per_symbol,
        trace=trace,
        overlays_raw=raw,
    )


def apply_decision_to_holdings(
    holdings: Iterable[HoldingRow] | None,
    decision: Decision,
) -> list[HoldingRow]:
    """Replace executable weight/action with the Decision; keep tape metadata.

    Names the engine wanted to enter but the process blocked stay in the
    table at weight 0 so the UI can show they were considered.
    """
    by_symbol = {row.symbol: row for row in decision.per_symbol}
    out: list[HoldingRow] = []
    for row in holdings or []:
        symbol = str(row.symbol or "").strip().upper()
        decided = by_symbol.get(symbol)
        if decided is None:
            continue
        out.append(
            HoldingRow(
                symbol=row.symbol,
                weight=float(decided.weight),
                action=decided.action if decided.action in ("enter", "exit", "hold", "add", "trim") else "hold",
                price=row.price,
                data_as_of=row.data_as_of,
                provisional=row.provisional,
                prev_weight=row.prev_weight,
                trend=row.trend,
            )
        )
    out.sort(key=lambda item: (-abs(float(item.weight)), item.symbol))
    return out


def _mandate_gate(
    rules: ProcessMandate,
    *,
    regime: str | None,
    stance: str,
    provisional: bool,
    horizons: Mapping[str, Any] | None = None,
) -> tuple[bool, float, TraceStep]:
    """Constitution first: shorts, bear, provisional bars, empty stance."""
    allow_new = True
    size_cap = float(rules.book_nav_cap)
    veto: str | None = None
    thoughts: list[str] = [
        f"这本主题账最多占净值 {rules.book_nav_cap * 100:.0f}%，单票不超过 {rules.max_name_weight * 100:.0f}%。",
        "只允许做多，不允许做空。",
    ]

    if not rules.allow_short:
        thoughts.append("空头方向被宪法关掉，后面任何 overlay 都不能把它打开。")

    if provisional and not rules.provisional_is_decision:
        allow_new = False
        veto = "mandate:provisional"
        thoughts.append(
            "今天的价格是盘中临时代理，还不是官方收盘。用临时价去新开，等于把回测（收盘）和指导（盘中）变成两套策略。"
        )
        conclusion = "盘中只预警，不授权新开。已有仓可以继续显示。"
        return allow_new, size_cap, TraceStep(
            gate="mandate",
            title="过程宪法",
            thought=" ".join(thoughts),
            conclusion=conclusion,
            passed=False,
            veto=veto,
        )

    if regime == "bear":
        allow_new = False
        veto = "mandate:bear"
        long_regime = _horizon_leg(horizons, "long")
        # No horizons blob → keep the old flatten (tests / legacy snapshots).
        flatten = long_regime in (None, "bear")
        if flatten:
            size_cap = 0.0
            thoughts.append(
                "沪深300 中期偏空。"
                "长期也偏空或尚未分类：今天不新买，建议规模收到 0%。"
                "这不是做空，也不是宣布熊市。"
            )
            conclusion = "今天先别买新的。0% 是不新买的上限，不是券商一键清仓。"
        else:
            thoughts.append(
                "沪深300 中期偏空，但年线尚未转空。"
                "只关新买；已有仓按个股拿着或减，不把整本账收到 0%。"
            )
            conclusion = "今天先别买新的。年线未破，已有仓可以续持/减仓，不是清仓。"
        return allow_new, size_cap, TraceStep(
            gate="mandate",
            title="过程宪法",
            thought=" ".join(thoughts),
            conclusion=conclusion,
            passed=False,
            veto=veto,
        )

    if stance == "none" and regime != "chop":
        # F0 already said none (e.g. no scored board). Chop+none still goes
        # through later gates so leftover-book copy can surface.
        thoughts.append("开仓资格层已经是「暂无推荐」，宪法先把新开关掉。")

    conclusion = "宪法允许往下走：还可以看结构、开仓资格和短期情景。"
    return allow_new, size_cap, TraceStep(
        gate="mandate",
        title="过程宪法",
        thought=" ".join(thoughts),
        conclusion=conclusion,
        passed=True,
    )


def _structure_gate(
    rows: list[HoldingRow],
    *,
    allow_new: bool,
    size_cap: float,
    regime: str | None,
    horizons: Mapping[str, Any] | None = None,
) -> tuple[bool, float, str, TraceStep]:
    """Index × stock MA stack: highest veto for adding."""
    actions = [_structure_action(row) for row in rows]
    present = [item for item in actions if item]
    book = "mixed"
    if regime == "bull":
        # Empirical rule on this book: index bull does not lift it — do not add.
        allow_new = False
        book = "index_bull"
        step = TraceStep(
            gate="structure",
            title="均线结构",
            thought=(
                "沪深300 走多，并不等于这本主题票会跟着涨。"
                "历史回看里指数多头阶段这本账 60 日上涨比例偏低，所以多头不是加仓理由。"
            ),
            conclusion="指数多头：不加仓。已有相对强势可以拿着。",
            passed=False,
            veto="structure:index_bull",
        )
        return allow_new, size_cap, book, step

    if regime == "bear":
        allow_new = False
        book = "index_bear"
        flatten = _horizon_leg(horizons, "long") in (None, "bear")
        if flatten:
            size_cap = 0.0
            thought = "指数中期偏空。长期也偏空或未分类时，反弹更像卖出窗口，今天不新买。"
            conclusion = "继续禁止新买。0% 不是做空，也不是宣布熊市。"
        else:
            thought = "指数中期偏空，但年线尚未转空。反弹仍宜减不宜追，已有仓不必整本清零。"
            conclusion = "继续禁止新买。已有仓按个股拿/减。"
        step = TraceStep(
            gate="structure",
            title="均线结构",
            thought=thought,
            conclusion=conclusion,
            passed=False,
            veto="structure:index_bear",
        )
        return allow_new, size_cap, book, step

    knives = sum(1 for item in present if item in _STRUCTURE_CUTS)
    fades = sum(1 for item in present if item == "sell_rally")
    adds = sum(1 for item in present if item in _STRUCTURE_ALLOWS_ADD)

    if present and adds == 0 and (knives or fades):
        allow_new = False
        book = "fade"
        step = TraceStep(
            gate="structure",
            title="均线结构",
            thought=(
                f"持仓里没有「拿住/低吸」，却有刀口 {knives} 只、高抛 {fades} 只。"
                "在震荡里，这表示价格已经贵或趋势向下，新开是在追。"
            ),
            conclusion="结构否决加仓。已有仓允许续持或减，不新开。",
            passed=False,
            veto="structure:fade",
        )
        return allow_new, size_cap, book, step

    thought = (
        "指数震荡时，这本账才按低吸高抛来做。"
        f"当前可分类持仓中，允许拿住/低吸 {adds} 只。"
    )
    if not present:
        thought = "还没有持仓上的结构标签（可能尚未加载）。结构这一关不额外否决，交给开仓资格。"
    return allow_new, size_cap, book, TraceStep(
        gate="structure",
        title="均线结构",
        thought=thought,
        conclusion="结构允许继续考虑这本账，但单票若在高抛/刀口，后面仍会禁该票新开。",
        passed=True,
    )


def _f0_gate(
    stance: str,
    *,
    allow_new: bool,
    size_cap: float,
    mandate: ProcessMandate,
) -> tuple[bool, float, TraceStep]:
    """F0 stance is eligibility, not a 'go buy full' order."""
    if stance == "none":
        allow_new = False
        size_cap = 0.0
        return allow_new, size_cap, TraceStep(
            gate="f0",
            title="开仓资格",
            thought=(
                "板块均价、确认覆盖或残仓门没有给出资格。"
                "没有资格时，「满仓」这种词根本不该出现在可执行结论里。"
            ),
            conclusion="没有开仓资格。规模上限收成 0。",
            passed=False,
            veto="f0:none",
        )
    if stance == "half_watch":
        size_cap = min(size_cap, mandate.book_nav_cap * mandate.half_watch_mult)
        allow_new = False
        return allow_new, size_cap, TraceStep(
            gate="f0",
            title="开仓资格",
            thought=(
                "资格是半仓/观望：意思是这本账还可以被持有，但今天不要把规模做大。"
                "把它读成「去买满」是错的。"
            ),
            conclusion=f"禁止新开/加仓。规模上限收到净值的 {size_cap * 100:.0f}%。",
            passed=False,
            veto="f0:half_watch",
        )
    # full = eligible to hold the book, still subject to structure/analog.
    return allow_new, size_cap, TraceStep(
        gate="f0",
        title="开仓资格",
        thought=(
            "板块侧给出满仓资格，只表示「这本账今天有资格被持有」，"
            "不是自动下单买满。后面的结构和短期情景仍可否决单票新开。"
        ),
        conclusion="资格通过，规模上限仍受宪法约束。",
        passed=True,
    )


def _analog_gate(
    analog_map: Mapping[str, AnalogCall],
    *,
    analog_applied: bool,
) -> TraceStep:
    if not analog_applied:
        return TraceStep(
            gate="analog",
            title="短期情景",
            thought=(
                "日频扫描不算 10 日形态匹配。那套计算又慢又容易变成另一套意见。"
                "没有它时，这一关跳过——跳过不等于看多。"
            ),
            conclusion="未使用短期情景。不因此多买，也不因此少买。",
            passed=True,
            skipped=True,
        )
    blocked = [symbol for symbol, call in analog_map.items() if call.blocks_new_risk()]
    if not blocked:
        return TraceStep(
            gate="analog",
            title="短期情景",
            thought=(
                "用过去相似的 20 日走势，看随后 10 个交易日更常涨还是更常跌。"
                "今天没有高确信的偏空调用，所以不否决新开。"
                "即使有偏多，也不会单独变成买入指令。"
            ),
            conclusion="短期情景未否决。",
            passed=True,
        )
    listed = "、".join(blocked[:8])
    return TraceStep(
        gate="analog",
        title="短期情景",
        thought=(
            "找到了高确信的偏空（或被指数情景强行反向）的名字。"
            "短期情景只能挡新开，不能因为「看起来要涨」而要求买入。"
        ),
        conclusion=f"禁止这些名字今日新开/加仓：{listed}。",
        passed=False,
        veto="analog:down",
    )


def _decide_symbols(
    rows: list[HoldingRow],
    *,
    allow_new: bool,
    analog_blocked: set[str],
    analog_map: Mapping[str, AnalogCall],
    max_name_weight: float,
) -> list[SymbolDecision]:
    decided: list[SymbolDecision] = []
    for row in rows:
        symbol = str(row.symbol or "").strip().upper()
        if not symbol:
            continue
        raw_weight = float(row.weight or 0.0)
        raw_action = str(row.action or "hold").strip().lower() or "hold"
        structure = _structure_action(row)
        call = analog_map.get(symbol)
        analog_bias = call.bias if call is not None else None
        had_position = row.prev_weight is not None and abs(float(row.prev_weight)) > WEIGHT_EPSILON
        currently_on = abs(raw_weight) > WEIGHT_EPSILON

        block_reason: str | None = None
        if structure and structure not in _STRUCTURE_ALLOWS_ADD:
            block_reason = f"structure:{structure}"
        if call is not None and call.blocks_new_risk():
            block_reason = "analog:down"
        if not allow_new and raw_action in _ADD_ACTIONS:
            block_reason = block_reason or "process:no_new"

        blocked_add = bool(block_reason) or (not allow_new)

        action: DecisionAction
        weight = raw_weight
        blocked = False

        if structure in _STRUCTURE_CUTS and currently_on:
            action = "trim"
            # Keep a remainder so the name does not vanish mid-scan; size gate
            # may still scale the book down to the cap.
        elif raw_action in _ADD_ACTIONS and blocked_add:
            blocked = True
            if had_position or (currently_on and raw_action == "add"):
                action = "hold"
                # Keep existing size; do not honour the add.
                if row.prev_weight is not None and abs(float(row.prev_weight)) > WEIGHT_EPSILON:
                    weight = float(row.prev_weight)
            else:
                # New enter vetoed — show as considered, executable weight 0.
                action = "hold"
                weight = 0.0
        elif raw_action == "exit":
            action = "exit"
            weight = 0.0
        elif raw_action == "trim":
            action = "trim"
        elif raw_action == "add" and not blocked_add:
            action = "add"
        elif raw_action == "enter" and not blocked_add:
            action = "enter"
        else:
            action = "hold"

        if abs(weight) > max_name_weight:
            weight = max_name_weight if weight > 0 else -max_name_weight
            blocked = True
            block_reason = block_reason or "size:max_name"

        decided.append(
            SymbolDecision(
                symbol=symbol,
                action=action,
                weight=float(weight),
                raw_weight=raw_weight,
                raw_action=raw_action,
                structure_action=structure,
                analog_bias=analog_bias,
                blocked=blocked,
                block_reason=block_reason,
            )
        )
    return decided


def _size_gate(
    per_symbol: list[SymbolDecision],
    *,
    size_cap: float,
) -> tuple[list[SymbolDecision], float, TraceStep]:
    """Scale the book down to the cap; never scale up above the engine."""
    cap = max(0.0, float(size_cap))
    live = [row for row in per_symbol if row.action != "exit"]
    gross = sum(abs(float(row.weight)) for row in live)
    if cap <= 0:
        for row in per_symbol:
            if row.action != "exit":
                if abs(row.weight) > WEIGHT_EPSILON and row.action in _ADD_ACTIONS:
                    row.action = "hold"
                row.weight = 0.0
                if row.raw_action in _ADD_ACTIONS:
                    row.blocked = True
                    row.block_reason = row.block_reason or "size:zero"
        return per_symbol, 0.0, TraceStep(
            gate="size",
            title="规模裁剪",
            thought="前面的关卡已经把可交易规模收到 0，这里只是把目标权重清零，避免页面上还留着「新入」。",
            conclusion="主题账占净值 0%。",
            passed=False,
            veto="size:zero",
        )

    scale = 1.0
    if gross > cap + 1e-12:
        scale = cap / gross
        for row in per_symbol:
            row.weight = float(row.weight) * scale
        gross = cap

    return per_symbol, gross, TraceStep(
        gate="size",
        title="规模裁剪",
        thought=(
            "引擎给的权重加总如果超过宪法上限，就按比例缩小，不会为了「买满」再加杠杆。"
            f"当前上限 {cap * 100:.0f}%，裁剪系数 {scale:.2f}。"
        ),
        conclusion=f"建议后的主题账约占净值 {gross * 100:.1f}%。",
        passed=True,
    )


def _portfolio_action(per_symbol: list[SymbolDecision], *, size_nav: float) -> DecisionAction:
    actions = [row.action for row in per_symbol if abs(float(row.weight)) > WEIGHT_EPSILON or row.action == "exit"]
    live_weights = [row for row in per_symbol if abs(float(row.weight)) > WEIGHT_EPSILON]
    if size_nav <= WEIGHT_EPSILON and not live_weights:
        if any(row.action == "exit" for row in per_symbol):
            return "exit"
        return "wait"
    if any(row.action == "enter" for row in live_weights):
        return "enter"
    if any(row.action == "add" for row in live_weights):
        return "add"
    if any(row.action == "exit" for row in per_symbol) and not live_weights:
        return "exit"
    if any(row.action == "trim" for row in per_symbol):
        return "trim"
    if live_weights:
        return "hold"
    return "wait"


def _tighten_against_previous(
    previous: Decision,
    *,
    action: DecisionAction,
    size_nav: float,
    per_symbol: list[SymbolDecision],
) -> tuple[DecisionAction, float, list[SymbolDecision], str | None]:
    """Analog (or any second pass) may only shrink, never reopen."""
    note_bits: list[str] = []
    prev_size = max(0.0, float(previous.size_nav))
    if size_nav > prev_size + 1e-9:
        if prev_size <= 0:
            for row in per_symbol:
                row.weight = 0.0
                if row.action in _ADD_ACTIONS:
                    row.action = "hold"
                    row.blocked = True
            size_nav = 0.0
        else:
            scale = prev_size / size_nav
            for row in per_symbol:
                row.weight = float(row.weight) * scale
            size_nav = prev_size
        note_bits.append("规模被压回日频扫描时的上限。")
    if previous.action == "wait" and action in ("enter", "add"):
        action = "wait"
        for row in per_symbol:
            if row.action in _ADD_ACTIONS:
                row.action = "hold"
                row.weight = 0.0
                row.blocked = True
                row.block_reason = row.block_reason or "tighten:wait"
        size_nav = 0.0
        note_bits.append("上次已经是等待，这次不能改成买入。")
    if not note_bits:
        return action, size_nav, per_symbol, None
    return action, size_nav, per_symbol, " ".join(note_bits)


def _compose_reason(
    *,
    action: DecisionAction,
    size_nav: float,
    allow_new: bool,
    vetoed_by: list[str],
    per_symbol: list[SymbolDecision],
    analog_applied: bool,
    provisional: bool,
) -> str:
    verb = {
        "wait": "今天先别买新的",
        "hold": "续持，不加仓",
        "enter": "有条件新开",
        "add": "有条件加仓",
        "trim": "减仓，不新开",
        "exit": "退出",
    }.get(action, action)
    blocked = [row.symbol for row in per_symbol if row.blocked and row.raw_action in _ADD_ACTIONS]
    bits = [verb, f"建议规模 {size_nav * 100:.1f}% 净值"]
    if provisional:
        bits.append("盘中临时价，不当正式日频决策")
    if not allow_new:
        bits.append("新开已关闭")
    if blocked:
        bits.append("否决新开 " + "、".join(blocked[:6]))
    if analog_applied:
        bits.append("已叠加短期情景")
    elif vetoed_by:
        bits.append("否决层 " + "，".join(vetoed_by[:4]))
    if "mandate:bear" in vetoed_by and size_nav <= WEIGHT_EPSILON:
        bits.append("0% 是今天不新买的上限，不是熊市清仓")
    return "；".join(bits) + "。"


def _horizon_leg(horizons: Mapping[str, Any] | None, key: str) -> str | None:
    """Read one short/medium/long label off the optional horizons blob."""
    if not isinstance(horizons, Mapping):
        return None
    value = str(horizons.get(key) or "").strip().lower()
    if value in ("bull", "chop", "bear"):
        return value
    return None


def _structure_action(row: HoldingRow) -> str | None:
    trend = row.trend
    if not isinstance(trend, dict):
        return None
    action = str(trend.get("action") or "").strip().lower()
    return action or None
