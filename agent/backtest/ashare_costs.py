"""A-share cost and fill assumptions shared by the runner and the monitor.

Monitor report refresh re-benches a bound run through
:func:`backtest.runner._create_market_engine`, which builds a
:class:`backtest.engines.china_a.ChinaAEngine`. That engine merges this module
into its config, so the board's backtest, a direct ``python -m backtest.runner``
A-share run, and the paper book (``src.paper_book``, which fills through
``ChinaAEngine``) use the same numbers.

Retail cash defaults:

* commission 万2.5 both sides, minimum ¥5 (``commission_rate`` / ``commission_min``)
* stamp tax 万5, sell side only (``stamp_tax``)
* transfer fee 万0.1 both sides (``transfer_fee``)
* slippage 0.1% (``slippage``)
* T+1 (``t_plus_one``)
* 涨停不买、跌停不卖 (engine heuristic, or the tradability panel when present)
* 停牌 / 零成交不成交 (``block_suspension``)

Explicit keys already on the backtest config win. Environment variables win
over these defaults when the config omits the key. See ``DataConfig`` in
``src.config.env_schema`` for the variable names
(``VIBE_TRADING_ASHARE_COMMISSION_RATE``, ``VIBE_TRADING_ASHARE_STAMP_TAX``,
``VIBE_TRADING_ASHARE_SLIPPAGE``, ``VIBE_TRADING_ASHARE_TPLUS1``,
``VIBE_TRADING_ASHARE_BLOCK_SUSPENSION``, ``VIBE_TRADING_TRADABILITY_PANEL``).
"""

from __future__ import annotations

from typing import Any

# Keys ChinaAEngine reads. Kept in one place so the runner, the monitor's
# re-bench, and a hand-built engine cannot drift.
COST_KEYS = (
    "commission_rate",
    "commission_min",
    "stamp_tax",
    "transfer_fee",
    "slippage",
    "t_plus_one",
    "block_suspension",
)

_FALLBACK = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax": 0.0005,
    "transfer_fee": 0.00001,
    "slippage": 0.001,
    "t_plus_one": True,
    "block_suspension": True,
}

_ENV_FIELDS = {
    "commission_rate": "vibe_trading_ashare_commission_rate",
    "commission_min": "vibe_trading_ashare_commission_min",
    "stamp_tax": "vibe_trading_ashare_stamp_tax",
    "transfer_fee": "vibe_trading_ashare_transfer_fee",
    "slippage": "vibe_trading_ashare_slippage",
    "t_plus_one": "vibe_trading_ashare_tplus1",
    "block_suspension": "vibe_trading_ashare_block_suspension",
}


def resolved_defaults() -> dict[str, Any]:
    """Cost/fill defaults after environment overrides.

    A config failure falls back to the retail constants above so a backtest
    still has a fee schedule.
    """
    defaults = dict(_FALLBACK)
    try:
        from src.config.accessor import get_env_config

        data = get_env_config().data
    except Exception:  # noqa: BLE001 — constants are the documented default
        return defaults
    for key, field in _ENV_FIELDS.items():
        if hasattr(data, field):
            defaults[key] = getattr(data, field)
    return defaults


def merge_assumptions(config: dict | None) -> dict:
    """Copy of *config* with missing cost/fill keys filled in.

    Keys the caller already set (including an explicit ``0``) are kept.
    ``tradability_index`` and every other engine key pass through untouched.
    """
    merged = dict(config or {})
    for key, value in resolved_defaults().items():
        merged.setdefault(key, value)
    return merged
