"""Bound-backtest expectation: equity path and fill prices on the run directory."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_equity(run_dir: Path | None) -> pd.Series | None:
    """Equity series indexed by ``YYYY-MM-DD``. Missing artifacts yield None."""
    if run_dir is None:
        return None
    path = Path(run_dir) / "artifacts" / "equity.csv"
    if not path.is_file():
        return None
    frame = pd.read_csv(path)
    column = next((name for name in ("equity", "nav", "value") if name in frame.columns), None)
    if column is None or frame.empty:
        return None
    date_col = next((name for name in ("timestamp", "date", "trade_date") if name in frame.columns), None)
    if date_col is None:
        date_col = frame.columns[0]
    dates = frame[date_col].map(_date_key)
    values = pd.to_numeric(frame[column], errors="coerce")
    series = pd.Series(values.to_numpy(), index=dates)
    series = series[~series.index.duplicated(keep="last")].dropna()
    series = series[series > 0]
    if series.empty:
        return None
    return series.sort_index()


def choose_anchor(equity: pd.Series, as_of: str) -> tuple[str, float] | None:
    """Capital base for cumulative-return comparison.

    Prefer the last backtest mark strictly before the paper book's first
    session, so day-one expected return is that session's backtest return.
    If the curve starts on ``as_of``, that row is the base and day-one
    expected return is zero.
    """
    if equity is None or equity.empty:
        return None
    prior = equity[equity.index < as_of]
    if not prior.empty:
        date = str(prior.index[-1])
        return date, float(prior.iloc[-1])
    if as_of in equity.index:
        return as_of, float(equity.loc[as_of])
    return None


def expectation_metrics(
    equity: pd.Series | None,
    *,
    as_of: str,
    anchor_date: str | None,
    anchor_equity: float | None,
    initial_cash: float,
    paper_cum_return: float,
    paper_daily_return: float,
) -> dict:
    """Scale the bound curve onto ``initial_cash`` and measure the path gap."""
    payload: dict = {
        "available": False,
        "anchor_date": anchor_date,
        "anchor_equity": anchor_equity,
        "expected_nav": None,
        "expected_cum_return": None,
        "expected_daily_return": None,
        "return_deviation": None,
        "daily_return_gap": None,
        "slippage_gap": None,
    }
    if equity is None or anchor_equity is None or anchor_equity <= 0 or as_of not in equity.index:
        return payload
    current = float(equity.loc[as_of])
    expected_cum = current / anchor_equity - 1.0
    payload["available"] = True
    payload["expected_cum_return"] = expected_cum
    payload["expected_nav"] = initial_cash * (1.0 + expected_cum)
    payload["return_deviation"] = abs(paper_cum_return - expected_cum)

    prior = equity[equity.index < as_of]
    base = float(prior.iloc[-1]) if not prior.empty else float(anchor_equity)
    if base > 0:
        expected_daily = current / base - 1.0
        payload["expected_daily_return"] = expected_daily
        payload["daily_return_gap"] = paper_daily_return - expected_daily
    return payload


def slippage_gap(run_dir: Path | None, as_of: str, fills: list[dict]) -> float | None:
    """Mean signed (paper fill - backtest fill) / backtest fill for this session.

    ``None`` when the run has no trade on the overlapping symbol and side.
    """
    if run_dir is None or not fills:
        return None
    path = Path(run_dir) / "artifacts" / "trades.csv"
    if not path.is_file():
        return None
    frame = pd.read_csv(path)
    needed = {"timestamp", "code", "side", "price"}
    if not needed.issubset(frame.columns) or frame.empty:
        return None
    frame = frame.copy()
    frame["day"] = frame["timestamp"].map(_date_key)
    day = frame[frame["day"] == as_of]
    if day.empty:
        return None
    gaps: list[float] = []
    for fill in fills:
        side = str(fill.get("side") or "")
        symbol = str(fill.get("symbol") or "").upper()
        paper_price = fill.get("fill_price")
        try:
            paper_price = float(paper_price)
        except (TypeError, ValueError):
            continue
        matched = day[(day["code"].astype(str).str.upper() == symbol) & (day["side"].astype(str) == side)]
        if matched.empty:
            continue
        backtest_price = float(pd.to_numeric(matched["price"], errors="coerce").mean())
        if backtest_price == 0 or backtest_price != backtest_price:
            continue
        gaps.append((paper_price - backtest_price) / backtest_price)
    if not gaps:
        return None
    return sum(gaps) / len(gaps)


def _date_key(value) -> str:
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return text[:10]
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(None)
    return stamp.strftime("%Y-%m-%d")
