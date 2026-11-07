"""A-share (China mainland) backtest engine.

Market rules:
  - T+1: cannot sell shares bought today
  - No short selling for retail investors
  - Price limits: ±10% main board, ±20% ChiNext/STAR, ±5% ST
  - Minimum lot: 100 shares (odd lots can only be sold, not bought)
  - Commission: ¥5 minimum, 0.025% bilateral
  - Stamp tax: 0.05% sell-side only
  - Transfer fee: 0.001% bilateral
"""

from __future__ import annotations

import pandas as pd

from backtest.engines.base import BaseEngine


class ChinaAEngine(BaseEngine):
    """A-share market engine.

    Config keys (see :mod:`backtest.ashare_costs`; env overrides apply when
    the key is omitted):
      - commission_rate: default 0.00025 (万2.5)
      - commission_min: default 5.0 (RMB)
      - stamp_tax: default 0.0005 (万5, sell-only)
      - transfer_fee: default 0.00001 (万0.1)
      - slippage: default 0.001
      - t_plus_one: default True. Set False or ``VIBE_TRADING_ASHARE_TPLUS1=0``
        to allow same-day sells.
      - block_suspension: default True. Zero volume, or a panel halt, does not
        fill. ``VIBE_TRADING_ASHARE_BLOCK_SUSPENSION=0`` turns that off.
      - tradability_index: optional :class:`src.data.tradability.TradabilityIndex`.
        When supplied, halt flags and exact price bands come from the panel;
        when absent the engine falls back to the close-to-close heuristic
        plus the zero-volume halt above. The runner attaches the panel by
        default (``VIBE_TRADING_TRADABILITY_PANEL=0`` to skip).
    """

    def __init__(self, config: dict):
        from backtest.ashare_costs import merge_assumptions

        # Shared with the monitor re-bench. Explicit config keys win over env.
        config = merge_assumptions(config)
        config = {**config, "leverage": 1.0}  # A-shares: no leverage
        super().__init__(config)
        self.commission_rate: float = float(config.get("commission_rate", 0.00025))
        self.commission_min: float = float(config.get("commission_min", 5.0))
        self.stamp_tax: float = float(config.get("stamp_tax", 0.0005))
        self.transfer_fee: float = float(config.get("transfer_fee", 0.00001))
        self.slippage_rate: float = float(config.get("slippage", 0.001))
        self.t_plus_one: bool = bool(config.get("t_plus_one", True))
        self.block_suspension: bool = bool(config.get("block_suspension", True))
        self.tradability_index = config.get("tradability_index")

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        """A-share execution rules.

        Args:
            symbol: Stock code (e.g. 000001.SZ).
            direction: 1 (buy), -1 (short — always blocked), 0 (sell/close).
            bar: Current bar (needs 'close', 'pre_close' or 'pct_chg').

        Returns:
            True if the trade is allowed.
        """
        # 1. No short selling
        if direction == -1:
            return False

        # 2. T+1: can't sell shares bought today. Disable with t_plus_one=False
        #    or VIBE_TRADING_ASHARE_TPLUS1=0 on the runner/monitor path.
        if self.t_plus_one and direction == 0:
            pos = self.positions.get(symbol)
            if pos is not None:
                bar_date = _bar_date(bar)
                entry_date = pos.entry_time.date() if hasattr(pos.entry_time, "date") else None
                if bar_date is not None and entry_date is not None and bar_date == entry_date:
                    return False

        # 3. Tradability panel (halt + exact price bands), when available.
        #    Authoritative: a halted name is untradable regardless of price, and
        #    the band comes from the exchange's own limit prices rather than a
        #    close-to-close approximation.
        panel_row = self._tradability_row(symbol, bar)
        if panel_row is not None:
            return self._panel_allows(direction, bar, panel_row)

        # 4. No panel: a zero-volume (or missing-volume) bar is a halt. The
        #    exchange published no trade, so the order cannot fill.
        if self.block_suspension and _bar_suspended(bar):
            return False

        # 5. Fallback heuristic: price limits from the bar's own move
        return self._heuristic_allows(symbol=symbol, direction=direction, bar=bar)

    def _tradability_row(self, symbol: str, bar: pd.Series):
        """Return the panel row for this order, or None when the panel can't answer."""
        store = getattr(self, "tradability_index", None)
        if store is None:
            return None
        day = _bar_date(bar)
        if day is None:
            return None
        try:
            return store.get(symbol, day)
        except Exception:  # noqa: BLE001 — a broken panel must not block trading
            return None

    def _panel_allows(self, direction: int, bar: pd.Series, row: dict) -> bool:
        """Apply the panel's halt flag and limit prices to one order."""
        if self.block_suspension and _row_suspended(row):
            return False

        # ``band_mismatch`` means the band contradicts the bar's own move: the
        # source was adjusted (dividend/split) or the bar is corrupt. Trust the
        # halt flag above, but leave the band to the heuristic in that case.
        if bool(_panel_value(row, "band_mismatch")):
            return self._heuristic_allows(symbol=row.get("symbol"), direction=direction, bar=bar)

        close = bar.get("close")
        try:
            close = None if close is None or pd.isna(close) else float(close)
        except (TypeError, ValueError):
            close = None

        if close is not None:
            # Half a cent of tolerance: limit prices are rounded to the tick, so
            # an exact equality test misses a close that sits on the band.
            limit_up = _panel_value(row, "limit_up")
            limit_down = _panel_value(row, "limit_down")
            if direction == 1 and limit_up is not None and close >= limit_up - 0.005:
                return False
            if direction == 0 and limit_down is not None and close <= limit_down + 0.005:
                return False
        return True

    def _heuristic_allows(self, *, symbol: str, direction: int, bar: pd.Series) -> bool:
        """Pre-panel price-limit rule, kept for rows the panel cannot vouch for."""
        pct_chg = _calc_pct_change(bar)
        if pct_chg is None:
            return True
        limit = _price_limit(symbol)
        if direction == 1 and pct_chg >= limit - 0.001:
            return False
        if direction == 0 and pct_chg <= -limit + 0.001:
            return False
        return True


    def round_size(self, raw_size: float, price: float) -> float:
        """Round down to 100-share lots."""
        return max(int(raw_size / 100) * 100, 0)

    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        """A-share fee structure: commission + stamp tax (sell) + transfer fee.

        ``_direction`` is unused today — reserved for future asymmetric
        long/short fee schedules (margin trading, securities lending).
        """
        notional = size * price
        # Commission: 万2.5, min ¥5
        comm = max(notional * self.commission_rate, self.commission_min)
        # Transfer fee: 万0.1 bilateral
        comm += notional * self.transfer_fee
        # Stamp tax: 万5 sell-only
        if not is_open:
            comm += notional * self.stamp_tax
        return comm

    def apply_slippage(self, price: float, direction: int) -> float:
        """A-share slippage (relatively small due to tick size)."""
        return price * (1 + direction * self.slippage_rate)


# ── Helpers ──


def _bar_date(bar: pd.Series):
    """Extract date from bar, handling various column names."""
    for col in ("trade_date", "date"):
        if col in bar.index:
            val = bar[col]
            if hasattr(val, "date"):
                return val.date()
            try:
                return pd.Timestamp(val).date()
            except Exception:
                pass
    # Fall back to bar name (index timestamp)
    if hasattr(bar, "name") and hasattr(bar.name, "date"):
        return bar.name.date()
    return None


# Note: china_futures and global_futures have variants that prioritise
# settle/pre_settle (futures-native); see those modules for the
# futures-specific logic.
def _calc_pct_change(bar: pd.Series):
    """Calculate price change percentage from bar data."""
    if "pct_chg" in bar.index:
        val = bar["pct_chg"]
        if pd.notna(val):
            return float(val) / 100.0  # tushare pct_chg is in percentage points

    close = bar.get("close")
    pre_close = bar.get("pre_close")
    if close is not None and pre_close is not None and pre_close > 0:
        return (float(close) - float(pre_close)) / float(pre_close)
    return None


def _price_limit(symbol: str, is_st: bool = False) -> float:
    """Determine the daily price band from the board (and ST status).

    Delegates to :func:`src.data.tradability.price_limit_pct` so the band table
    lives in one place.  ``is_st`` matters: ST names run a 5% band and the code
    prefix cannot reveal that, which is why the parameter is explicit rather
    than guessed.

    Args:
        symbol: Stock code (e.g. 300001.SZ, 688001.SH, 000001.SZ).
        is_st: True when the name carries an ST/*ST designation that day.

    Returns:
        Limit as fraction (0.05, 0.10, 0.20 or 0.30).
    """
    from src.data.tradability import price_limit_pct

    return price_limit_pct(symbol, is_st=is_st)


def _row_suspended(row: dict) -> bool:
    """True when the panel marks the session halted.

    ``is_suspended`` is a bool on a freshly built panel and may come back as
    0/1 from parquet. ``_panel_value`` would coerce that through ``float``,
    which is the wrong type for a flag, so the halt check stays here.
    """
    value = row.get("is_suspended")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    try:
        if value is None or pd.isna(value):
            return False
    except (TypeError, ValueError):
        return bool(value)
    return bool(value)


def _bar_suspended(bar: pd.Series) -> bool:
    """True when the bar itself shows no trade (halt / 停牌).

    A missing ``volume`` column is not evidence — some callers build a price
    bar without it. A present volume that is NaN or <= 0 is a session the
    exchange did not trade.
    """
    if "volume" not in getattr(bar, "index", []):
        return False
    volume = bar.get("volume")
    try:
        if volume is None or pd.isna(volume):
            return True
        return float(volume) <= 0
    except (TypeError, ValueError):
        return True


def _panel_value(row: dict, key: str):
    """Read a numeric panel field, normalising NaN/missing to None."""
    value = row.get(key)
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
