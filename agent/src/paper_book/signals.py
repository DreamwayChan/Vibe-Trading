"""Turn a monitor board into tradable order intents for one session."""

from __future__ import annotations

from src.monitor.models import WEIGHT_EPSILON, MonitorConfig
from src.paper_book.models import Intent


def resolve_intents(
    config: MonitorConfig,
    as_of: str,
) -> tuple[list[Intent], list[dict[str, str]]]:
    """Buys and sells whose suggestions are in the tradable pool for ``as_of``.

    Holds are not orders. A buy and a sell on the same name collapse to the
    sell. Weights come from the board's holdings when that name has one;
    otherwise the remaining budget is split evenly across the buys.
    """
    skipped: list[dict[str, str]] = []
    sells: list[str] = []
    buys: list[str] = []
    seen: set[str] = set()

    for suggestion in config.suggestions:
        action = suggestion.action
        if action not in ("buy", "sell"):
            continue
        symbol = (suggestion.symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        if suggestion.as_of and suggestion.as_of != as_of:
            skipped.append({"symbol": symbol, "action": action, "reason": "stale_signal"})
            continue
        if suggestion.provisional:
            skipped.append({"symbol": symbol, "action": action, "reason": "provisional"})
            continue
        if not suggestion.tradable:
            reason = ",".join(suggestion.block_reasons) or "not_tradable"
            skipped.append({"symbol": symbol, "action": action, "reason": reason})
            continue
        seen.add(symbol)
        if action == "sell":
            sells.append(symbol)
        else:
            buys.append(symbol)

    sell_set = set(sells)
    buys = [symbol for symbol in buys if symbol not in sell_set]
    weights = _buy_weights(config, buys)
    intents = [Intent(symbol=symbol, action="sell", target_weight=0.0) for symbol in sorted(sells)]
    intents.extend(
        Intent(symbol=symbol, action="buy", target_weight=weights.get(symbol, 0.0))
        for symbol in sorted(buys)
    )
    return intents, skipped


def _buy_weights(config: MonitorConfig, buys: list[str]) -> dict[str, float]:
    if not buys:
        return {}
    explicit: dict[str, float] = {}
    holding_weights = {
        row.symbol.strip().upper(): float(row.weight)
        for row in config.holdings
        if row.symbol and row.weight > WEIGHT_EPSILON
    }
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
    remain = max(0.0, 1.0 - specified)
    share = remain / len(unspecified) if unspecified else 0.0
    weights = dict(explicit)
    for symbol in unspecified:
        weights[symbol] = share
    total = sum(weights.values())
    if total > 1.0:
        weights = {symbol: weight / total for symbol, weight in weights.items()}
    return weights
