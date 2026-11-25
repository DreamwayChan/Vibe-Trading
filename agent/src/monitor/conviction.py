"""Five-dimension conviction funnel (五维共振聚焦器) for personal A-share trading.

Personal traders with limited capital and attention cannot hold 6-10 rotated
names at once. Default focus is 2-3 highest-conviction setups where:
  1. Market climate (CSI 300 + whole-market breadth) is supportive;
  2. Sector wind (Market Pick rank / score) is in the leading tier;
  3. Stock setup is a healthy pullback / early breakout (not an overextended chase or knife);
  4. Strategy validation & paper-book soak show intact edge (no decay);
  5. A-share risk & event calendar are clean (no ST, 3-board mania, or imminent mine).

Everything here is 100% deterministic, rule-based, and auditable. No LLM sits in
this scoring path.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from src.data.breadth import BreadthSnapshot
from src.monitor.models import HoldingRow, MonitorConfig, MonitorSuggestion

logger = logging.getLogger(__name__)

FocusTier = Literal["focus", "watch", "avoid"]

DEFAULT_MAX_FOCUS = 3
MIN_FOCUS_STARS = 3.2


@dataclass
class ConvictionItem:
    """Explainable 5-dimension conviction card for one candidate symbol."""

    symbol: str
    action: str
    stars: float
    tier: FocusTier
    is_focus: bool
    vetoed: bool = False
    veto_reasons: list[str] = field(default_factory=list)
    climate_score: float = 0.5
    sector_score: float = 0.5
    setup_score: float = 0.5
    track_score: float = 0.5
    safety_score: float = 1.0
    board_name: str | None = None
    board_rank: int | None = None
    bias_ma20: float | None = None
    evidence: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> ConvictionItem | None:
        if not isinstance(raw, Mapping):
            return None
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        tier = str(raw.get("tier") or "watch").strip().lower()
        if tier not in ("focus", "watch", "avoid"):
            tier = "watch"
        return cls(
            symbol=symbol,
            action=str(raw.get("action") or "hold"),
            stars=float(raw.get("stars") or 0.0),
            tier=tier,  # type: ignore[arg-type]
            is_focus=bool(raw.get("is_focus", False)),
            vetoed=bool(raw.get("vetoed", False)),
            veto_reasons=[str(x) for x in (raw.get("veto_reasons") or []) if str(x).strip()],
            climate_score=float(raw.get("climate_score") or 0.0),
            sector_score=float(raw.get("sector_score") or 0.0),
            setup_score=float(raw.get("setup_score") or 0.0),
            track_score=float(raw.get("track_score") or 0.0),
            safety_score=float(raw.get("safety_score") or 0.0),
            board_name=str(raw["board_name"]) if raw.get("board_name") else None,
            board_rank=int(raw["board_rank"]) if raw.get("board_rank") is not None else None,
            bias_ma20=float(raw["bias_ma20"]) if raw.get("bias_ma20") is not None else None,
            evidence=[str(x) for x in (raw.get("evidence") or []) if str(x).strip()],
            cautions=[str(x) for x in (raw.get("cautions") or []) if str(x).strip()],
        )


@dataclass
class ConvictionSummary:
    """Board-level funnel result narrowing candidates down to 2-3 focus picks."""

    as_of: str
    max_focus: int
    focus_symbols: list[str] = field(default_factory=list)
    stance: str = "watch"
    headline: str = ""
    breadth: dict[str, Any] | None = None
    items: list[ConvictionItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "max_focus": self.max_focus,
            "focus_symbols": list(self.focus_symbols),
            "stance": self.stance,
            "headline": self.headline,
            "breadth": dict(self.breadth) if isinstance(self.breadth, Mapping) else None,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> ConvictionSummary | None:
        if not isinstance(raw, Mapping):
            return None
        as_of = str(raw.get("as_of") or "").strip()
        if not as_of:
            return None
        items: list[ConvictionItem] = []
        for entry in raw.get("items") or []:
            parsed = ConvictionItem.from_dict(entry if isinstance(entry, Mapping) else None)
            if parsed is not None:
                items.append(parsed)
        breadth = raw.get("breadth")
        return cls(
            as_of=as_of,
            max_focus=int(raw.get("max_focus") or DEFAULT_MAX_FOCUS),
            focus_symbols=[str(s).upper() for s in (raw.get("focus_symbols") or []) if str(s).strip()],
            stance=str(raw.get("stance") or "watch"),
            headline=str(raw.get("headline") or ""),
            breadth=dict(breadth) if isinstance(breadth, Mapping) else None,
            items=items,
        )


def build_conviction_summary(
    config: MonitorConfig,
    *,
    as_of: str,
    data_map: Mapping[str, pd.DataFrame] | None = None,
    breadth: BreadthSnapshot | Mapping[str, Any] | None = None,
    sector_ranks: Mapping[str, tuple[str, int, int]] | None = None,
    paper_window: Mapping[str, Any] | None = None,
    event_flags_by_symbol: Mapping[str, Sequence[str]] | None = None,
    max_focus: int = DEFAULT_MAX_FOCUS,
) -> ConvictionSummary:
    """Score active holdings & buy suggestions, selecting up to ``max_focus`` picks."""
    max_focus = max(1, min(int(max_focus or DEFAULT_MAX_FOCUS), 5))
    breadth_snap = (
        breadth
        if isinstance(breadth, BreadthSnapshot)
        else BreadthSnapshot.from_dict(breadth)
    )
    if breadth_snap is None and data_map:
        from src.data.breadth import compute_breadth_from_map

        breadth_snap = compute_breadth_from_map(data_map, as_of=as_of)

    sector_map = (
        dict(sector_ranks)
        if sector_ranks is not None
        else _load_cached_sector_ranks(as_of)
    )
    window = dict(paper_window) if isinstance(paper_window, Mapping) else _load_paper_window(config)
    events = event_flags_by_symbol or {}

    climate_val, climate_veto, climate_note = _score_climate(config, breadth_snap)
    track_val, track_note, track_warn = _score_track_record(config, window)

    candidates = _collect_candidates(config)
    scored: list[ConvictionItem] = []
    for symbol, action, holding, suggestion in candidates:
        sec_val, board_name, board_rank, sec_note = _score_sector(symbol, sector_map)
        frame = (data_map or {}).get(symbol)
        setup_val, bias20, setup_note, setup_warn = _score_setup(frame, holding)
        safe_val, safe_vetoes, safe_warns = _score_safety(
            suggestion=suggestion,
            event_flags=list(events.get(symbol) or []),
        )

        veto_reasons = list(safe_vetoes)
        if climate_veto and action in ("buy", "enter", "add"):
            veto_reasons.append(climate_veto)

        evidence: list[str] = []
        cautions: list[str] = []
        if climate_note:
            (evidence if climate_val >= 0.6 else cautions).append(climate_note)
        if sec_note:
            (evidence if sec_val >= 0.65 else cautions).append(sec_note)
        if setup_note:
            evidence.append(setup_note)
        if setup_warn:
            cautions.append(setup_warn)
        if track_note:
            evidence.append(track_note)
        if track_warn:
            cautions.append(track_warn)
        cautions.extend(safe_warns)

        raw_stars = climate_val + sec_val + setup_val + track_val + safe_val
        if veto_reasons:
            raw_stars = min(raw_stars, 2.2)
        stars = round(float(np.clip(raw_stars, 0.0, 5.0)), 2)

        scored.append(
            ConvictionItem(
                symbol=symbol,
                action=action,
                stars=stars,
                tier="avoid" if veto_reasons else "watch",
                is_focus=False,
                vetoed=bool(veto_reasons),
                veto_reasons=veto_reasons,
                climate_score=round(climate_val, 2),
                sector_score=round(sec_val, 2),
                setup_score=round(setup_val, 2),
                track_score=round(track_val, 2),
                safety_score=round(safe_val, 2),
                board_name=board_name,
                board_rank=board_rank,
                bias_ma20=round(bias20, 4) if bias20 is not None else None,
                evidence=evidence,
                cautions=cautions,
            )
        )

    # Sort highest stars first; prefer fresh enter/buy/hold over trim/exit.
    scored.sort(
        key=lambda item: (
            0 if not item.vetoed else 1,
            -item.stars,
            item.symbol,
        )
    )

    focus_symbols: list[str] = []
    for item in scored:
        if item.action in ("sell", "exit"):
            continue
        if (
            not item.vetoed
            and item.stars >= MIN_FOCUS_STARS
            and len(focus_symbols) < max_focus
        ):
            item.is_focus = True
            item.tier = "focus"
            focus_symbols.append(item.symbol)
        elif not item.vetoed and item.stars < 2.5:
            item.tier = "avoid"

    stance, headline = _build_headline(
        focus_symbols=focus_symbols,
        scored=scored,
        climate_veto=climate_veto,
        breadth=breadth_snap,
        max_focus=max_focus,
    )
    return ConvictionSummary(
        as_of=as_of,
        max_focus=max_focus,
        focus_symbols=focus_symbols,
        stance=stance,
        headline=headline,
        breadth=breadth_snap.to_dict() if breadth_snap is not None else None,
        items=scored,
    )


def _collect_candidates(
    config: MonitorConfig,
) -> list[tuple[str, str, HoldingRow | None, MonitorSuggestion | None]]:
    hold_map = {h.symbol.strip().upper(): h for h in config.holdings if h.symbol}
    sugg_map = {s.symbol.strip().upper(): s for s in config.suggestions if s.symbol}
    ordered: list[str] = []
    for sym in [*hold_map.keys(), *sugg_map.keys()]:
        if sym and sym not in ordered:
            ordered.append(sym)
    out: list[tuple[str, str, HoldingRow | None, MonitorSuggestion | None]] = []
    for sym in ordered:
        h = hold_map.get(sym)
        s = sugg_map.get(sym)
        if h is not None:
            action = h.action
        elif s is not None:
            action = s.action
        else:
            continue
        if h is None and s is not None and s.action == "hold" and s.position != "long":
            continue
        out.append((sym, action, h, s))
    return out


def _score_climate(
    config: MonitorConfig,
    breadth: BreadthSnapshot | None,
) -> tuple[float, str | None, str]:
    decision = config.decision
    regime = None
    horizons: dict[str, Any] = {}
    if decision is not None:
        raw = decision.overlays_raw if hasattr(decision, "overlays_raw") else {}
        if isinstance(raw, Mapping):
            regime = raw.get("market_regime")
            h = raw.get("horizons")
            if isinstance(h, Mapping):
                horizons = dict(h)

    long_reg = horizons.get("long")
    if regime == "bear" and long_reg in (None, "bear"):
        return 0.0, "market_bear", "沪深300 中长期偏空，大盘气候否决新开仓"
    if breadth is not None and breadth.sufficient_sample and breadth.state == "cold":
        return 0.1, "breadth_cold", "全市场处于亏钱退潮冰点，暂停追开新仓"

    score = 0.65
    notes: list[str] = []
    if regime == "bear":
        score = 0.35
        notes.append("沪深300 中期偏弱（年线尚在），仅宜轻仓防守")
    elif regime == "bull":
        score = 0.75
        notes.append("沪深300 趋势走多")
    else:
        score = 0.70
        notes.append("指数震荡期，适合结构性低吸")

    if breadth is not None and breadth.sufficient_sample:
        if breadth.state == "warm":
            score = min(1.0, score + 0.25)
            notes.append(f"全市场赚钱效应偏暖（上涨 {breadth.adv_ratio * 100:.0f}%）")
        elif breadth.stance_cap == "half_watch":
            score = max(0.35, score - 0.20)
            notes.append("全市场赚钱效应分化，控制半仓以内")

    return score, None, "；".join(notes)


def _score_sector(
    symbol: str,
    sector_map: Mapping[str, tuple[str, int, int]],
) -> tuple[float, str | None, int | None, str]:
    hit = sector_map.get(symbol)
    if hit is None:
        return 0.55, None, None, "未命中本地近期板块扫描快照（板块顺风按中性计）"
    name, rank, total = hit
    total = max(1, total)
    pct = rank / total
    if rank <= 3 or pct <= 0.10:
        return 1.0, name, rank, f"所属【{name}】位列全市场前排（#{rank}/{total}，主线强顺风）"
    if pct <= 0.25:
        return 0.80, name, rank, f"所属【{name}】处于强势梯队（#{rank}/{total}）"
    if pct <= 0.50:
        return 0.55, name, rank, f"所属【{name}】排名中游（#{rank}/{total}）"
    return 0.25, name, rank, f"所属【{name}】排名偏后（#{rank}/{total}，非当前风口）"


def _score_setup(
    frame: pd.DataFrame | None,
    holding: HoldingRow | None,
) -> tuple[float, float | None, str | None, str | None]:
    trend = holding.trend if holding is not None and isinstance(holding.trend, Mapping) else {}
    t_action = str(trend.get("action") or "")

    if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
        if t_action in ("buy_dip", "hold"):
            return 0.75, None, "趋势结构健康（允许持有/低吸）", None
        if t_action in ("reduce", "sell_rally"):
            return 0.25, None, None, "趋势结构提示逢高减仓/刀口规避"
        return 0.60, None, "技术结构中性", None

    closes = pd.to_numeric(frame["close"], errors="coerce").dropna().to_numpy(dtype=float)
    if len(closes) < 20:
        return 0.60, None, "上市或窗口不足20日，形态中性", None

    last = float(closes[-1])
    ma10 = float(np.mean(closes[-10:]))
    ma20 = float(np.mean(closes[-20:]))
    ma60 = float(np.mean(closes[-60:])) if len(closes) >= 60 else ma20
    bias20 = (last - ma20) / ma20 if ma20 > 0 else 0.0

    if t_action == "reduce" or (last < ma20 and ma20 < ma60):
        return 0.15, bias20, None, f"均线空头或走弱（偏离MA20 {bias20 * 100:+.1f}%），不接下降飞刀"
    if bias20 > 0.15:
        return 0.25, bias20, None, f"短期距MA20乖离过大（{bias20 * 100:+.1f}%），追高盈亏比差"
    if -0.02 <= bias20 <= 0.08 and last >= ma60 * 0.98:
        if last >= ma10:
            return 1.0, bias20, f"多头趋势温和启动/贴近均线（偏离MA20 {bias20 * 100:+.1f}%），买点舒服", None
        return 0.90, bias20, f"中期向上、回踩MA20支撑带（偏离MA20 {bias20 * 100:+.1f}%），低吸赔率佳", None
    if 0.08 < bias20 <= 0.15:
        return 0.65, bias20, f"趋势偏强但有一定涨幅（偏离MA20 {bias20 * 100:+.1f}%），宜分批不追高", None
    return 0.50, bias20, f"震荡整理区间（偏离MA20 {bias20 * 100:+.1f}%）", None


def _score_track_record(
    config: MonitorConfig,
    window: Mapping[str, Any] | None,
) -> tuple[float, str | None, str | None]:
    gate = config.validation_gate or {}
    gate_passed = bool(gate.get("passed"))
    if not gate_passed:
        return 0.30, None, "绑定回测尚未通过验证门（仅供研究观察）"

    if isinstance(window, Mapping) and window:
        decay = window.get("decay") if isinstance(window.get("decay"), Mapping) else {}
        if bool(decay.get("decayed")) or bool(window.get("downgraded")):
            return 0.20, None, "纸面账本近期出现边缘衰减或超阈偏离，警惕策略失效"
        sessions = int(window.get("session_count") or 0)
        if sessions >= 10:
            return 1.0, f"计成本验证门已过 + 纸面浸泡 {sessions} 日表现稳健", None

    if config.tradable:
        return 0.85, "已通过五栏假设与计成本统计验证门", None
    return 0.65, "计成本验证门已通过（待补齐可交易日志）", None


def _score_safety(
    *,
    suggestion: MonitorSuggestion | None,
    event_flags: list[str],
) -> tuple[float, list[str], list[str]]:
    flags = set((suggestion.risk_flags if suggestion is not None else []) or [])
    vetoes: list[str] = []
    warns: list[str] = []

    if "st" in flags:
        vetoes.append("ashare_st")
        warns.append("ST/*ST 风险警示股，硬性排除")
    if "one_word" in flags or "limit_up" in flags:
        vetoes.append("ashare_limit_up")
        warns.append("当日涨停/一字板，不追涨停板")
    if "extreme_board" in flags:
        vetoes.append("ashare_extreme_board")
        warns.append("三连板以上情绪极值，严防高位接力核按钮")
    elif "consecutive_board" in flags:
        warns.append("已连续两日涨停，短线波动加剧")

    for ev in event_flags:
        if ev.startswith("veto:"):
            label = ev.split(":", 1)[1]
            vetoes.append(label)
            warns.append(f"事件排雷拦截：{label}")
        else:
            warns.append(ev)

    if vetoes:
        return 0.0, vetoes, warns
    if warns:
        return 0.60, [], warns
    return 1.0, [], []


def _build_headline(
    *,
    focus_symbols: list[str],
    scored: list[ConvictionItem],
    climate_veto: str | None,
    breadth: BreadthSnapshot | None,
    max_focus: int,
) -> tuple[str, str]:
    if climate_veto == "market_bear":
        return "defensive", "大盘中长期偏空：今日不新开仓，守住现金等右侧信号。"
    if climate_veto == "breadth_cold":
        return "defensive", "全市场亏钱效应明显（退潮冰点）：今日管住手不新买，避免逆势挨刀。"
    if not scored:
        return "empty", "当前策略池无持仓与新信号，保持空仓观望。"
    if not focus_symbols:
        return (
            "wait",
            "虽有候选信号，但五维共振均未达 3.2 星高胜率线——宁可错过，不做勉强交易。",
        )
    joined = "、".join(focus_symbols)
    if breadth is not None and breadth.sufficient_sample and breadth.stance_cap == "half_watch":
        return (
            "selective",
            f"市场分化期精选聚焦：仅关注前 {len(focus_symbols)} 只共振标的（{joined}），总仓控制在半仓内。",
        )
    return (
        "actionable",
        f"五维共振通过：精力聚焦 Top {len(focus_symbols)}（上限 {max_focus} 只）—— {joined}。",
    )


def _load_cached_sector_ranks(as_of: str) -> dict[str, tuple[str, int, int]]:
    """Best-effort offline lookup of symbol -> (best_board_name, rank, total_ranked_boards)."""
    try:
        from src.market_pick.store import list_snapshot_dates, load_day_snapshot, market_pick_dir

        dates = [d for d in list_snapshot_dates() if d <= as_of]
        if not dates:
            return {}
        snap = load_day_snapshot(dates[-1])
        if snap is None or not snap.boards:
            return {}
        ranked = [b for b in snap.boards if b.rank is not None]
        if not ranked:
            return {}
        total = len(ranked)
        ranked.sort(key=lambda b: int(b.rank or 9999))

        members_root = market_pick_dir() / "members"
        if not members_root.is_dir():
            return {}

        import json

        out: dict[str, tuple[str, int, int]] = {}
        for board in ranked:
            # Find most recent cached member list for this board without network calls.
            files = sorted(members_root.glob(f"{board.bk}_*.json"))
            if not files:
                continue
            try:
                payload = json.loads(files[-1].read_text(encoding="utf-8"))
                syms = payload.get("symbols") if isinstance(payload, dict) else payload
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(syms, list):
                continue
            for raw_sym in syms:
                sym = str(raw_sym or "").strip().upper()
                if sym and sym not in out:
                    out[sym] = (board.name, int(board.rank or total), total)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("conviction cached sector rank lookup skipped: %s", exc)
        return {}


def _load_paper_window(config: MonitorConfig) -> dict[str, Any] | None:
    run_id = str(getattr(config.strategy, "run_id", "") or "").strip()
    if not run_id:
        return None
    try:
        from src.paper_book.store import load_window, paper_book_root

        root = paper_book_root()
        if not (root / run_id / "reports" / "window.json").is_file():
            return None
        return load_window(root, run_id)
    except Exception:  # noqa: BLE001
        return None
