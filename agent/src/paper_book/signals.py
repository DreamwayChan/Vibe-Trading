"""Turn a monitor board into tradable order intents for one session."""

from __future__ import annotations

from src.monitor.models import WEIGHT_EPSILON, MonitorConfig
from src.monitor.uncausal import UNCAUSAL_SCAN, board_decision_record, is_uncausal_scan
from src.paper_book.models import Intent


def _decision_by_symbol(config: MonitorConfig) -> dict[str, object]:
    """Map uppercase symbol -> SymbolDecision when a Process Decision is attached."""
    decision = getattr(config, "decision", None)
    if decision is None:
        return {}
    per_symbol = getattr(decision, "per_symbol", None)
    if per_symbol is None and isinstance(decision, dict):
        per_symbol = decision.get("per_symbol")
    out: dict[str, object] = {}
    for item in per_symbol or []:
        symbol = (
            item.get("symbol")
            if isinstance(item, dict)
            else getattr(item, "symbol", None)
        )
        key = str(symbol or "").strip().upper()
        if key:
            out[key] = item
    return out


def _dec_field(entry: object, key: str, default: object = None) -> object:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _matches_exec_session(suggestion: object, as_of: str) -> bool:
    """True when suggestion targets execution session ``as_of``.

    Settled monitor scans stamp ``exec_as_of`` to the next trading session
    (T+1 open). Explicit schedules and legacy fixtures leave ``exec_as_of``
    unset and match on ``as_of`` directly.
    """
    exec_day = getattr(suggestion, "exec_as_of", None)
    if exec_day:
        return str(exec_day) == as_of
    sig_day = getattr(suggestion, "as_of", None)
    return not sig_day or str(sig_day) == as_of


def resolve_intents(
    config: MonitorConfig,
    as_of: str,
) -> tuple[list[Intent], list[dict[str, str]]]:
    """Buys and sells whose suggestions are in the tradable pool for ``as_of``.

    Holds are not orders unless ProcessMandate tightened them into ``exit``.
    A buy and a sell on the same name collapse to the sell. Weights come from
    ``config.decision.per_symbol`` when present (enforcing ProcessMandate caps),
    else from the board's holdings; unspecified buys split the remaining budget.
    """
    skipped: list[dict[str, str]] = []
    sells: list[str] = []
    buys: list[str] = []
    seen: set[str] = set()

    run_id = str(getattr(getattr(config, "strategy", None), "run_id", "") or "")
    if is_uncausal_scan(run_id, decided=board_decision_record(config)):
        # A schedule that omits ``tradable`` still defaults it to true.
        # That default must not fill a frozen 「最优」 recipe. A board that
        # carries a decision record (gate pass + journal linked to a live
        # hypothesis) is not a recipe and falls through to the row checks.
        for suggestion in config.suggestions:
            action = suggestion.action
            if action not in ("buy", "sell"):
                continue
            symbol = (suggestion.symbol or "").strip().upper()
            if not symbol:
                continue
            skipped.append({"symbol": symbol, "action": action, "reason": UNCAUSAL_SCAN})
        return [], skipped

    dec_map = _decision_by_symbol(config)
    decision_obj = getattr(config, "decision", None)
    decision_action = (
        decision_obj.get("action")
        if isinstance(decision_obj, dict)
        else getattr(decision_obj, "action", None)
    )
    decision_size_nav = (
        decision_obj.get("size_nav")
        if isinstance(decision_obj, dict)
        else getattr(decision_obj, "size_nav", None)
    )

    for suggestion in config.suggestions:
        symbol = (suggestion.symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        dec = dec_map.get(symbol)
        dec_act = str(_dec_field(dec, "action", "") or "").lower() if dec is not None else ""
        action = suggestion.action
        # Process waterfall can tighten a hold/buy into an exit (e.g. long-bear flatten).
        if dec_act == "exit" and action in ("hold", "buy"):
            action = "sell"
        if action not in ("buy", "sell"):
            continue
        if not _matches_exec_session(suggestion, as_of):
            reason = (
                "next_session_signal"
                if getattr(suggestion, "exec_as_of", None)
                and str(suggestion.exec_as_of) > as_of
                else "stale_signal"
            )
            skipped.append({"symbol": symbol, "action": action, "reason": reason})
            continue
        if suggestion.provisional:
            skipped.append({"symbol": symbol, "action": action, "reason": "provisional"})
            continue
        if not suggestion.tradable:
            reason = ",".join(suggestion.block_reasons) or "not_tradable"
            skipped.append({"symbol": symbol, "action": action, "reason": reason})
            continue
        if action == "buy" and decision_obj is not None:
            if dec is not None:
                dec_w = float(_dec_field(dec, "weight", 0.0) or 0.0)
                if bool(_dec_field(dec, "blocked", False)) or dec_act in ("wait", "exit") or dec_w <= WEIGHT_EPSILON:
                    blk = str(_dec_field(dec, "block_reason", "") or "").strip() or "process_blocked"
                    skipped.append({"symbol": symbol, "action": "buy", "reason": blk})
                    continue
            elif decision_action == "wait" or (
                decision_size_nav is not None and float(decision_size_nav) <= WEIGHT_EPSILON
            ):
                skipped.append({"symbol": symbol, "action": "buy", "reason": "process_wait"})
                continue

        seen.add(symbol)
        if action == "sell":
            sells.append(symbol)
        else:
            buys.append(symbol)

    # When the board itself cleared the admission gate, include rotated strategy
    # enters/exits from holdings/ops even if the user's manual watchlist was a subset.
    if getattr(config, "tradable", False):
        board_exec_days = {
            str(s.exec_as_of) for s in config.suggestions if getattr(s, "exec_as_of", None)
        }
        exec_ok = (not board_exec_days) or (as_of in board_exec_days)
        board_provisional = any(s.provisional for s in config.suggestions) or any(
            h.provisional for h in config.holdings
        )
        if exec_ok and not board_provisional:
            for exited_sym in getattr(getattr(config, "ops", None), "exited", []) or []:
                sym = str(exited_sym or "").strip().upper()
                if sym and sym not in seen:
                    seen.add(sym)
                    sells.append(sym)
            for h_row in config.holdings:
                sym = str(h_row.symbol or "").strip().upper()
                if not sym or sym in seen or h_row.action not in ("enter", "add"):
                    continue
                dec = dec_map.get(sym)
                if decision_obj is not None and dec is not None:
                    dec_act = str(_dec_field(dec, "action", "") or "").lower()
                    dec_w = float(_dec_field(dec, "weight", 0.0) or 0.0)
                    if bool(_dec_field(dec, "blocked", False)) or dec_act in ("wait", "exit") or dec_w <= WEIGHT_EPSILON:
                        blk = str(_dec_field(dec, "block_reason", "") or "").strip() or "process_blocked"
                        skipped.append({"symbol": sym, "action": "buy", "reason": blk})
                        continue
                seen.add(sym)
                buys.append(sym)

    sell_set = set(sells)
    buys = [symbol for symbol in buys if symbol not in sell_set]
    weights = _buy_weights(config, buys, dec_map=dec_map)
    intents = [Intent(symbol=symbol, action="sell", target_weight=0.0) for symbol in sorted(sells)]
    intents.extend(
        Intent(symbol=symbol, action="buy", target_weight=weights.get(symbol, 0.0))
        for symbol in sorted(buys)
    )
    return intents, skipped


def _buy_weights(
    config: MonitorConfig,
    buys: list[str],
    *,
    dec_map: dict[str, object] | None = None,
) -> dict[str, float]:
    if not buys:
        return {}
    explicit: dict[str, float] = {}
    dec_lookup = dec_map or {}
    holding_weights = {
        row.symbol.strip().upper(): float(row.weight)
        for row in config.holdings
        if row.symbol and row.weight > WEIGHT_EPSILON
    }
    # ProcessMandate tightened weights override raw SignalEngine holding weights.
    for symbol, entry in dec_lookup.items():
        w = float(_dec_field(entry, "weight", 0.0) or 0.0)
        if w > WEIGHT_EPSILON:
            holding_weights[symbol] = w

    unspecified: list[str] = []
    for symbol in buys:
        weight = holding_weights.get(symbol)
        if weight is None:
            unspecified.append(symbol)
        else:
            explicit[symbol] = weight
    specified = sum(explicit.values())
    if specified > 1.0 and explicit:
        explicit = {symbol: weight / specified for symbol, weight in explicit.items()}
        specified = 1.0
    decision_obj = getattr(config, "decision", None)
    cap = 1.0
    if decision_obj is not None and dec_lookup:
        raw_cap = (
            decision_obj.get("size_nav")
            if isinstance(decision_obj, dict)
            else getattr(decision_obj, "size_nav", None)
        )
        if raw_cap is not None and float(raw_cap) > 0:
            cap = min(1.0, float(raw_cap))
    remain = max(0.0, cap - specified)
    share = remain / len(unspecified) if unspecified else 0.0
    weights = dict(explicit)
    for symbol in unspecified:
        weights[symbol] = share
    total = sum(weights.values())
    if total > 1.0:
        weights = {symbol: weight / total for symbol, weight in weights.items()}
    return weights
