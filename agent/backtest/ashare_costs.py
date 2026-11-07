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

The production default is this cost-aware schedule. A side-by-side view of the
same strategy with no fees and no limit/halt blocks uses the named
:data:`FRICTIONLESS_PROFILE` (``python -m backtest.cost_compare``). That
profile replaces config and environment keys; it does not change the default.
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


# Named comparison profile. ``backtest.cost_compare`` overlays these values
# verbatim so the frictionless leg cannot pick up ``VIBE_TRADING_ASHARE_*``
# or a fee key left on ``config.json``.
#
# Turned off:
#   commission_rate, commission_min, stamp_tax, transfer_fee, slippage
#   t_plus_one — same-day sells are allowed (the cost-aware default keeps T+1)
#   block_suspension — halted names and zero-volume bars may fill
#   enforce_price_limits — limit-up buys and limit-down sells may fill.
#     Both the tradability-panel band and the close-to-close heuristic are
#     skipped. A panel may still be attached; it is not consulted for bands
#     or halts while these two flags are false.
#
# Left on:
#   short sales stay blocked
#   buys still round down to 100-share lots
FRICTIONLESS_PROFILE: dict[str, Any] = {
    "commission_rate": 0.0,
    "commission_min": 0.0,
    "stamp_tax": 0.0,
    "transfer_fee": 0.0,
    "slippage": 0.0,
    "t_plus_one": False,
    "block_suspension": False,
    "enforce_price_limits": False,
}

PROFILE_FRICTIONLESS = "frictionless"
PROFILE_ASHARE_COSTS = "ashare_costs"


def apply_frictionless(config: dict | None) -> dict:
    """Copy of *config* with :data:`FRICTIONLESS_PROFILE` overlaid.

    Profile keys replace explicit config values and anything
    :func:`merge_assumptions` would fill from the environment. Other keys
    (codes, cash, ``tradability_index``) pass through.
    """
    merged = dict(config or {})
    merged.update(FRICTIONLESS_PROFILE)
    return merged


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
