"""Settle a calendar range of A-share sessions in one call.

``step_range`` walks exchange sessions (weekends and holidays skipped), reuses
:func:`src.paper_book.book.step_day` for each one, and writes a window report
beside the daily reports. A deviation downgrade on any session sticks for the
rest of the window.

CLI::

    python -m src.paper_book soak --days 20 --end YYYY-MM-DD --run-id <id>
    python -m src.paper_book soak --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>
    python -m src.paper_book step --from YYYY-MM-DD --to YYYY-MM-DD --run-id <id>

Each session reads that day's monitor snapshot when one is on disk, then the
live board when its suggestions are dated that session. ``--signals`` is a
JSON object keyed by session date (or a list of rows with ``as_of``) and
replaces both for a replay. A row trades when ``tradable`` is omitted or true.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from backtest.engines.china_a import ChinaAEngine

from src.monitor.models import HoldingRow, MonitorConfig, MonitorDaySnapshot, MonitorSuggestion
from src.hypotheses.journal import machine_levels
from src.paper_book.book import _default_book_id, _open_store, step_day
from src.paper_book.decay import DEFAULT_MAX_DECAY, edge_decay
from src.paper_book.downgrade import clear_for_decay
from src.paper_book.models import PAPER_BOOK_BLOCK_REASONS, PAPER_BOOK_DECAY, PAPER_BOOK_DEVIATION, WindowReport
from src.paper_book.store import (
    PaperBookError,
    load_report,
    load_state,
    paper_book_root,
    require_book_id,
    require_date,
    save_window,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SOAK_SESSIONS",
    "calendar_kind",
    "load_signal_schedule",
    "plan_window",
    "sessions_ending",
    "step_range",
    "trading_sessions",
]

DEFAULT_SOAK_SESSIONS = 20
# Enough calendar days to cover ``count`` sessions plus weekends and a holiday
# week. A-share closures run about ten sessions a year, not months.
_LOOKBACK_FACTOR = 3
_LOOKBACK_PAD_DAYS = 14
_UNSET = object()


def step_range(
    *,
    start: str,
    end: str,
    config: MonitorConfig,
    book_id: str | None = None,
    run_dir: Path | None = None,
    root: Path | str | None = None,
    sessions: Sequence[str] | None = None,
    calendar: str | None = None,
    bars_by_day: Mapping[str, Mapping[str, pd.Series]] | None = None,
    configs_by_day: Mapping[str, MonitorConfig] | None = None,
    signals_by_day: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    threshold: float | None = None,
    initial_cash: float | None = None,
    max_position: float | None = None,
    max_drawdown: float | None = None,
    engine: ChinaAEngine | None = None,
    strategy_store: Any = _UNSET,
    use_snapshots: bool = True,
) -> WindowReport:
    """Step every session in ``[start, end]`` and persist the window report.

    Pass ``sessions`` only to replay a calendar the caller already resolved.
    Otherwise the exchange calendar selects the dates. Sessions already settled
    before the last one are left as stored. The latest session is restepped,
    same as a one-day ``step``.
    """
    start_day = require_date(start)
    end_day = require_date(end)
    if sessions is None:
        resolved = trading_sessions(start_day, end_day)
        label = calendar or calendar_kind()
    else:
        resolved = _explicit_sessions(start_day, end_day, sessions)
        label = calendar or "explicit"
    if not resolved:
        raise PaperBookError(f"no trading sessions in {start_day}..{end_day}")

    store_root = paper_book_root(root)
    resolved_id = require_book_id(book_id or _default_book_id(config))
    sources: dict[str, str] = {}
    downgraded = _board_blocked(config)
    pending = _pending_sessions(store_root, resolved_id, resolved)
    for day in pending:
        replay = _is_replay(store_root, resolved_id, day)
        day_config, source = _config_for_day(
            config,
            day,
            configs_by_day=configs_by_day,
            signals_by_day=signals_by_day,
            use_snapshots=use_snapshots,
        )
        if downgraded:
            day_config = _blocked(day_config, config)
        if replay:
            source = "replay"
        call: dict[str, Any] = {
            "as_of": day,
            "config": day_config,
            "book_id": resolved_id,
            "run_dir": run_dir,
            "root": store_root,
            "bars": _bars_for(day, bars_by_day),
            "threshold": threshold,
            "initial_cash": initial_cash,
            "max_position": max_position,
            "max_drawdown": max_drawdown,
            "engine": engine,
        }
        if strategy_store is not _UNSET:
            call["strategy_store"] = strategy_store
        report = step_day(**call)
        sources[day] = source
        if report.downgraded:
            downgraded = True
            _stamp_live_board(config, report.downgrade_reason or PAPER_BOOK_DEVIATION)

    run_id = (config.strategy.run_id or "").strip()
    window = _window_from_reports(
        store_root,
        book_id=resolved_id,
        run_id=run_id,
        sessions=resolved,
        calendar=label,
        sources=sources,
        board_downgraded=_board_blocked(config),
        max_decay=_decay_threshold(run_id),
    )
    store = strategy_store
    if store is _UNSET:
        store = _open_store()
    if clear_for_decay(
        config,
        window.decay,
        run_key=run_id or resolved_id,
        run_dir=str(run_dir) if run_dir is not None else None,
        as_of=window.end,
        strategy_store=store,
    ):
        window.downgraded = True
        if not window.downgrade_reason:
            window.downgrade_reason = PAPER_BOOK_DECAY
            window.downgraded_on = window.end
        window.decay = {**window.decay, "cleared_tradable": True}
    else:
        window.decay = {**window.decay, "cleared_tradable": False}
    save_window(store_root, window)
    logger.info(
        "paper book %s window %s..%s sessions=%d nav=%.2f deviation=%s downgraded=%s decayed=%s",
        resolved_id,
        window.start,
        window.end,
        window.session_count,
        window.nav,
        window.return_deviation,
        window.downgraded,
        window.decay.get("decayed"),
    )
    return window


def plan_window(
    *,
    start: str | None = None,
    end: str | None = None,
    days: int | None = None,
) -> tuple[str, str, list[str] | None]:
    """Resolve a from/to range or the last ``days`` sessions.

    Returns ``(start, end, sessions)``. ``sessions`` is set when the caller
    asked for a count, so the window is exactly that many sessions ending on
    or before ``end``. A from/to range leaves it ``None`` and
    :func:`trading_sessions` applies the calendar.
    """
    start_text = (start or "").strip()
    end_text = (end or "").strip()
    if days is not None and int(days) < 1:
        raise PaperBookError(f"session count must be positive, got {days}")
    if start_text and days is not None:
        raise PaperBookError("pass either --from/--to or --days/--end, not both")
    if start_text and end_text:
        return require_date(start_text), require_date(end_text), None
    if start_text and not end_text:
        raise PaperBookError("range step needs --to")
    if not end_text:
        raise PaperBookError("soak needs --from/--to or --end")
    count = DEFAULT_SOAK_SESSIONS if days is None else int(days)
    resolved = sessions_ending(end_text, count)
    return resolved[0], resolved[-1], resolved


def trading_sessions(start: str, end: str) -> list[str]:
    """Inclusive A-share sessions in ``[start, end]``. Weekends and holidays drop out."""
    start_day = require_date(start)
    end_day = require_date(end)
    if end_day < start_day:
        raise PaperBookError(f"session range {start_day} is after {end_day}")
    from src.data.calendar import trading_days

    return list(trading_days(start_day, end_day))


def sessions_ending(end: str, count: int) -> list[str]:
    """The ``count`` sessions ending on or before ``end``."""
    if count < 1:
        raise PaperBookError(f"session count must be positive, got {count}")
    end_day = require_date(end)
    span_days = count * _LOOKBACK_FACTOR + _LOOKBACK_PAD_DAYS
    start = (dt.date.fromisoformat(end_day) - dt.timedelta(days=span_days)).isoformat()
    found = trading_sessions(start, end_day)
    if len(found) < count:
        raise PaperBookError(f"need {count} sessions on or before {end_day}, calendar returned {len(found)}")
    return found[-count:]


def calendar_kind() -> str:
    """``exchange`` when a seeded calendar is loaded, otherwise ``weekday``."""
    from src.data.calendar import calendar_source, has_calendar

    if not has_calendar():
        return "weekday"
    source = calendar_source()
    if source in {"akshare", "cache"}:
        return "exchange"
    return "weekday"


def load_signal_schedule(path: Path | str) -> dict[str, list[dict[str, Any]]]:
    """Load a per-session suggestion schedule.

    Object form is ``{"YYYY-MM-DD": [ {symbol, action, weight?} ]}``. List form
    is rows with ``as_of`` (or ``date``). ``tradable`` defaults to true.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        raise PaperBookError(f"signal schedule not found: {file}")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperBookError(f"signal schedule unreadable: {exc}") from exc
    if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
        raw = raw["sessions"]
    out: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            day = require_date(str(key))
            if not isinstance(value, list):
                raise PaperBookError(f"signal schedule for {day} must be a list")
            out[day] = [item for item in value if isinstance(item, dict)]
        return out
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            day = require_date(str(item.get("as_of") or item.get("date") or ""))
            out.setdefault(day, []).append(item)
        return out
    raise PaperBookError("signal schedule must be a JSON object or list")


def _explicit_sessions(start: str, end: str, sessions: Sequence[str]) -> list[str]:
    out = [require_date(day) for day in sessions]
    if out != sorted(set(out)):
        raise PaperBookError("sessions must be unique and ascending")
    if out and (out[0] < start or out[-1] > end):
        raise PaperBookError("sessions fall outside the requested range")
    return out


def _pending_sessions(root: Path, book_id: str, sessions: list[str]) -> list[str]:
    state = load_state(root, book_id)
    settled = state.settled_through if state is not None else None
    if not settled:
        return list(sessions)
    pending = [day for day in sessions if day > settled]
    if not pending and settled == sessions[-1]:
        return [sessions[-1]]
    return pending


def _is_replay(root: Path, book_id: str, day: str) -> bool:
    state = load_state(root, book_id)
    return state is not None and state.settled_through == day


def _bars_for(
    day: str,
    bars_by_day: Mapping[str, Mapping[str, pd.Series]] | None,
) -> Mapping[str, pd.Series] | None:
    if bars_by_day is None:
        return None
    return bars_by_day.get(day, {})


def _config_for_day(
    config: MonitorConfig,
    day: str,
    *,
    configs_by_day: Mapping[str, MonitorConfig] | None,
    signals_by_day: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    use_snapshots: bool,
) -> tuple[MonitorConfig, str]:
    if configs_by_day is not None and day in configs_by_day:
        return configs_by_day[day], "schedule"
    if signals_by_day is not None and day in signals_by_day:
        return _config_from_signals(config, day, signals_by_day[day]), "schedule"
    if use_snapshots:
        snapshot = _load_snapshot(config, day)
        if snapshot is not None:
            return _config_from_snapshot(config, snapshot), "snapshot"
    if _board_applies(config, day):
        return config, "board"
    flat = MonitorConfig.from_dict(config.to_dict())
    flat.suggestions = []
    flat.holdings = []
    return flat, "flat"


def _board_applies(config: MonitorConfig, day: str) -> bool:
    for suggestion in config.suggestions:
        if suggestion.action not in ("buy", "sell"):
            continue
        if suggestion.as_of and suggestion.as_of != day:
            continue
        return True
    return False


def _load_snapshot(config: MonitorConfig, day: str) -> MonitorDaySnapshot | None:
    run_id = (config.strategy.run_id or "").strip()
    if not run_id:
        return None
    from src.monitor.store import is_safe_history_key, load_day_snapshot

    if not is_safe_history_key(run_id):
        return None
    # ``load_day_snapshot`` creates the history directory. Skip the call when
    # that day's file is absent so a soak does not mkdir on every flat session.
    path = Path.home() / ".vibe-trading" / "monitor" / "history" / run_id / f"{day}.json"
    if not path.is_file():
        return None
    return load_day_snapshot(run_id, day)


def _config_from_snapshot(base: MonitorConfig, snapshot: MonitorDaySnapshot) -> MonitorConfig:
    cfg = MonitorConfig.from_dict(base.to_dict())
    cfg.suggestions = list(snapshot.suggestions)
    cfg.holdings = list(snapshot.holdings)
    cfg.ops = snapshot.ops
    cfg.tradable = bool(snapshot.tradable)
    cfg.tradable_reasons = list(snapshot.tradable_reasons)
    cfg.validation_gate = snapshot.validation_gate
    return cfg


def _config_from_signals(
    base: MonitorConfig,
    day: str,
    rows: Sequence[Mapping[str, Any]],
) -> MonitorConfig:
    cfg = MonitorConfig.from_dict(base.to_dict())
    suggestions: list[MonitorSuggestion] = []
    holdings: list[HoldingRow] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        action = str(row.get("action") or "buy").strip().lower()
        if not symbol or action not in ("buy", "sell", "hold"):
            raise PaperBookError(f"bad signal on {day}: {row!r}")
        reasons = row.get("block_reasons") or []
        suggestions.append(
            MonitorSuggestion(
                symbol=symbol,
                action=action,  # type: ignore[arg-type]
                reason=str(row.get("reason") or "signal"),
                as_of=str(row.get("as_of") or day),
                tradable=bool(row.get("tradable", True)),
                provisional=bool(row.get("provisional", False)),
                block_reasons=[str(item) for item in reasons if str(item).strip()],
            )
        )
        weight = row.get("weight", row.get("target_weight"))
        if action == "buy" and weight is not None:
            holdings.append(HoldingRow(symbol=symbol, weight=float(weight)))
    cfg.suggestions = suggestions
    if holdings:
        cfg.holdings = holdings
    if any(item.tradable for item in suggestions):
        cfg.tradable = True
    return cfg


def _board_blocked(config: MonitorConfig) -> bool:
    return any(reason in PAPER_BOOK_BLOCK_REASONS for reason in (config.tradable_reasons or []))


def _active_block_reasons(config: MonitorConfig) -> list[str]:
    reasons = [reason for reason in (config.tradable_reasons or []) if reason in PAPER_BOOK_BLOCK_REASONS]
    return reasons or [PAPER_BOOK_DEVIATION]


def _blocked(config: MonitorConfig, live: MonitorConfig) -> MonitorConfig:
    """Copy that will not open or close. The downgrade on ``live`` already stuck."""
    cloned = MonitorConfig.from_dict(config.to_dict())
    cloned.tradable = False
    for reason in _active_block_reasons(live):
        if reason not in cloned.tradable_reasons:
            cloned.tradable_reasons.append(reason)
        for suggestion in cloned.suggestions:
            suggestion.tradable = False
            if reason not in suggestion.block_reasons:
                suggestion.block_reasons.append(reason)
    return cloned


def _stamp_live_board(config: MonitorConfig, reason: str) -> None:
    config.tradable = False
    if reason not in config.tradable_reasons:
        config.tradable_reasons.append(reason)
    for suggestion in config.suggestions:
        suggestion.tradable = False
        if reason not in suggestion.block_reasons:
            suggestion.block_reasons.append(reason)


def _decay_threshold(run_id: str) -> float:
    from src.config.accessor import get_env_config

    configured = float(get_env_config().data.vibe_trading_paper_book_max_decay)
    if configured <= 0 or configured != configured:
        configured = DEFAULT_MAX_DECAY
    if not run_id:
        return configured
    journal_cap = machine_levels(run_id).get("decay")
    if journal_cap is None:
        return configured
    return min(configured, journal_cap)


def _window_from_reports(
    root: Path,
    *,
    book_id: str,
    run_id: str,
    sessions: list[str],
    calendar: str,
    sources: dict[str, str],
    board_downgraded: bool,
    max_decay: float = DEFAULT_MAX_DECAY,
) -> WindowReport:
    days: list[dict[str, Any]] = []
    for day in sessions:
        raw = load_report(root, book_id, day)
        if raw is None:
            raise PaperBookError(f"missing daily report for {day}")
        days.append(_day_row(raw, sources.get(day, "stored")))
    last = days[-1]
    expectation = dict(last.get("expectation") or {})
    deviations: list[tuple[str, float]] = []
    for row in days:
        value = row.get("return_deviation")
        if value is None:
            continue
        try:
            deviations.append((str(row["as_of"]), float(value)))
        except (TypeError, ValueError):
            continue
    max_on: str | None = None
    max_dev: float | None = None
    for day, value in deviations:
        if max_dev is None or value > max_dev:
            max_dev = value
            max_on = day
    last_dev = last.get("return_deviation")
    try:
        last_dev = float(last_dev) if last_dev is not None else None
    except (TypeError, ValueError):
        last_dev = None
    downgraded_on = next((str(row["as_of"]) for row in days if row.get("downgraded")), None)
    downgraded = downgraded_on is not None or board_downgraded
    day_reason = next(
        (str(row.get("downgrade_reason")) for row in days if row.get("downgraded") and row.get("downgrade_reason")),
        None,
    )
    traded = sum(float(row.get("traded_notional") or 0.0) for row in days)
    turnover = sum(float(row.get("turnover") or 0.0) for row in days)
    return WindowReport(
        book_id=book_id,
        run_id=run_id or str(last.get("run_id") or ""),
        start=sessions[0],
        end=sessions[-1],
        sessions=list(sessions),
        session_count=len(sessions),
        calendar=calendar,
        nav=float(last.get("nav") or 0.0),
        cash=float(last.get("cash") or 0.0),
        market_value=float(last.get("market_value") or 0.0),
        initial_cash=float(last.get("initial_cash") or 0.0),
        cum_return=float(last.get("cum_return") or 0.0),
        turnover=turnover,
        traded_notional=traded,
        fill_count=sum(int(row.get("fill_count") or 0) for row in days),
        unfilled_count=sum(int(row.get("unfilled_count") or 0) for row in days),
        return_deviation=last_dev,
        max_return_deviation=max_dev,
        max_return_deviation_on=max_on,
        expectation=expectation,
        downgraded=downgraded,
        downgrade_reason=(day_reason or PAPER_BOOK_DEVIATION) if downgraded else None,
        downgraded_on=downgraded_on,
        days=days,
        decay=edge_decay(days, max_decay=max_decay),
    )


def _day_row(report: Mapping[str, Any], source: str) -> dict[str, Any]:
    fills = [item for item in (report.get("fills") or []) if isinstance(item, dict)]
    unfilled = [item for item in (report.get("unfilled") or []) if isinstance(item, dict)]
    expectation = report.get("expectation") if isinstance(report.get("expectation"), dict) else {}
    traded = 0.0
    for fill in fills:
        try:
            traded += float(fill.get("notional") or 0.0)
        except (TypeError, ValueError):
            continue
    return {
        "as_of": report.get("as_of"),
        "nav": report.get("nav"),
        "cash": report.get("cash"),
        "market_value": report.get("market_value"),
        "initial_cash": report.get("initial_cash"),
        "prev_nav": report.get("prev_nav"),
        "daily_return": report.get("daily_return"),
        "cum_return": report.get("cum_return"),
        "turnover": report.get("turnover"),
        "fills": fills,
        "unfilled": unfilled,
        "fill_count": len(fills),
        "unfilled_count": len(unfilled),
        "traded_notional": traded,
        "return_deviation": expectation.get("return_deviation"),
        "expected_cum_return": expectation.get("expected_cum_return"),
        "expected_nav": expectation.get("expected_nav"),
        "downgraded": bool(report.get("downgraded")),
        "downgrade_reason": report.get("downgrade_reason"),
        "signal_source": source,
        "expectation": dict(expectation),
    }
