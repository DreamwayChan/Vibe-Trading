"""Tick-level trade direction for A-shares (Tencent ``gtimg`` detail feed).

Why this exists
---------------
Every other free source in this repo gives OHLCV: what traded, not who
initiated. The one thing research cannot reconstruct from bars is **trade
direction** — how much of the volume was aggressive buying (主动买) versus
aggressive selling (主动卖). That is what separates "price went up" from
"price went up because buyers paid up".

The feed
--------
``stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600519&p=N``

Each page carries 70 prints as
``seq/time/price/change/volume(手)/amount(元)/side`` where ``side`` is
``B`` (buy-initiated), ``S`` (sell-initiated) or ``M`` (neutral/auction).
Paging stops when a page comes back empty.

Hard limits worth knowing before planning a backfill:

* **Only the most recent session is served.** The ``d=`` parameter is
  accepted and ignored, so history has to accumulate one day at a time.
* A liquid name is ~55–60 pages per session (70 prints each). A whole-market
  sweep is therefore ~300k requests/day, which is not viable; a watchlist of a
  few dozen names is.
* Volume is in 手 (100 shares). Amounts are元, already gross.

What gets stored
----------------
Not the prints themselves — a per-(symbol, session) aggregate, which is the
part that is usable as a factor::

    symbol  date  trades  volume  amount  vwap
    buy_volume  sell_volume  neutral_volume
    buy_amount  sell_amount  active_buy_ratio  net_active_amount
    big_amount  big_amount_ratio  big_net_amount  big_trade_count
    avg_trade_amount  post_close_volume  post_close_amount  prints_complete
    open_auction_volume  open_auction_amount
    close_auction_volume  close_auction_amount
    first_time  last_time

``active_buy_ratio`` and ``big_net_amount`` are the two columns this dataset
exists for: they turn "成交量放大" into "是谁在买".
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.request
from typing import Any, Iterable, Sequence

import pandas as pd

from src.data import lake

logger = logging.getLogger(__name__)

DATASET = "tick_agg"
_COLUMNS = [
    "symbol",
    "date",
    "trades",
    "volume",
    "amount",
    "vwap",
    "buy_volume",
    "sell_volume",
    "neutral_volume",
    "buy_amount",
    "sell_amount",
    "active_buy_ratio",
    "net_active_amount",
    "big_amount",
    "big_amount_ratio",
    "big_net_amount",
    "big_trade_count",
    "avg_trade_amount",
    "post_close_volume",
    "post_close_amount",
    "prints_complete",
    "open_auction_volume",
    "open_auction_amount",
    "close_auction_volume",
    "close_auction_amount",
    "first_time",
    "last_time",
    "source",
]

_BASE = "http://stock.gtimg.cn/data/index.php"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
_PAYLOAD_RE = re.compile(r'=\[.*?"(.*?)"', re.S)
_PAGE_SIZE = 70
_MAX_PAGES = 400
# A single print at or above this notional is treated as a block trade. Fixed
# rather than percentile-based so the column means the same thing across names
# and sessions — a moving threshold would make "big" incomparable over time.
_BIG_TRADE_AMOUNT = 1_000_000.0
# Close of the continuous session. The closing call auction prints as ONE
# aggregate batch stamped 15:00:01 (2,546手 on 600519 2026-10-30) and it is part
# of the day's volume, so the session must run through the whole 15:00 minute.
# Only 15:01+ prints (盘后固定价格交易, a handful of 100-share lots) are outside.
_SESSION_LAST = "15:00:59"
_POST_CLOSE_FIRST = "15:01:00"
_OPEN_AUCTION_BEFORE = "09:30:00"
_CLOSE_HOUR, _CLOSE_MINUTE = 15, 0
_CN_TZ = dt.timezone(dt.timedelta(hours=8))


def _tencent_code(symbol: str) -> str | None:
    """Map ``600519.SH`` to ``sh600519`` (None when not an A-share)."""
    parts = str(symbol).upper().split(".")
    if len(parts) != 2:
        return None
    code, suffix = parts
    if suffix == "SH":
        return f"sh{code}"
    if suffix == "SZ":
        return f"sz{code}"
    if suffix == "BJ":
        return f"bj{code}"
    return None


def _http_get(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("gbk", "ignore")


def _parse_page(payload: str) -> list[list[str]]:
    """Extract the ``|``-joined print records from one response body."""
    match = _PAYLOAD_RE.search(payload)
    if not match or not match.group(1).strip():
        return []
    return [row.split("/") for row in match.group(1).split("|") if row.count("/") >= 6]


def _fetch_page(code: str, page: int) -> list[list[str]]:
    """Fetch and parse one page of prints."""
    url = f"{_BASE}?appn=detail&action=data&c={code}&p={page}"
    return _parse_page(_http_get(url))


def fetch_ticks(
    symbol: str,
    *,
    max_pages: int = _MAX_PAGES,
    trade_date: str | None = None,
) -> pd.DataFrame:
    """Download the current session's prints for *symbol*.

    Args:
        symbol: A-share code such as ``600519.SH``.
        max_pages: Safety cap on pagination.
        trade_date: YYYY-MM-DD stamp to attach. Defaults to today in Beijing.

    Returns:
        Frame with ``time`` (DatetimeTZ-naive Beijing), ``price``, ``change``,
        ``volume`` (shares), ``amount`` (元), ``side`` and ``is_auction``.
        Empty when the symbol has no prints (halted, delisted, bad code).
    """
    code = _tencent_code(symbol)
    if not code:
        logger.debug("ticks: unsupported symbol %s", symbol)
        return pd.DataFrame()

    day = trade_date or dt.datetime.now(_CN_TZ).date().isoformat()
    rows: list[list[str]] = []
    pages = 0
    truncated = False
    for page in range(max_pages):
        try:
            page_rows = _fetch_page(code, page)
        except Exception as exc:  # noqa: BLE001 — one flaky page must be visible
            logger.warning("ticks: %s page %d failed (%s); result is incomplete", symbol, page, exc)
            truncated = True
            break
        if not page_rows:
            break
        # Pages are NOT uniformly full: a real session ends 70, 70, …, 69, 1.
        # A short page therefore proves nothing, and seq numbers skip
        # occasionally (601318.SH jumps 4758 → 4760 with volume still matching
        # the bar exactly), so neither is usable as a completeness signal.
        # Only an exception or the page cap can truncate; the caller's volume
        # reconciliation is the authoritative check.
        pages += 1
        rows.extend(page_rows)
    else:
        # Loop exhausted without a terminating empty page: the symbol has more
        # prints than max_pages and the tail was silently dropped.
        truncated = True
        logger.warning("ticks: %s hit max_pages=%d; tail missing", symbol, max_pages)

    if not rows:
        logger.info("ticks: no prints for %s on %s", symbol, day)
        empty = pd.DataFrame()
        empty.attrs["complete"] = False
        empty.attrs["pages"] = 0
        return empty

    frame = pd.DataFrame(
        [row[:7] for row in rows],
        columns=["seq", "clock", "price", "change", "lots", "amount", "side"],
    )
    frame["time"] = pd.to_datetime(
        day + " " + frame["clock"].astype(str), format="%Y-%m-%d %H:%M:%S", errors="coerce"
    )
    for column in ("price", "change", "lots", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["side"] = frame["side"].str.strip().str.upper()
    frame = frame.dropna(subset=["time", "price", "lots", "amount"])
    # Prints are 手; research columns are in shares so they line up with bars.
    frame["volume"] = frame["lots"] * 100.0
    frame["is_auction"] = frame["time"].dt.strftime("%H:%M:%S") < _OPEN_AUCTION_BEFORE
    # ``seq`` is unique per print. Deduplicating on (time, price, lots, side)
    # would delete genuine repeats — two 100-lot buys in the same second are
    # common — and silently understate volume by a few percent.
    frame = frame.drop_duplicates(subset=["seq"]).reset_index(drop=True)
    frame.attrs["complete"] = not truncated
    frame.attrs["pages"] = pages
    return frame


def aggregate(ticks: pd.DataFrame, symbol: str, trade_date: str | None = None) -> dict[str, Any]:
    """Collapse one session of prints into the factor-ready row.

    Args:
        ticks: Output of :func:`fetch_ticks`.
        symbol: Symbol the prints belong to.
        trade_date: Overrides the date taken from the prints.

    Returns:
        A single-row mapping matching :data:`_COLUMNS`, or ``{}`` when empty.
    """
    if ticks is None or ticks.empty:
        return {}

    # Read the flag before slicing: boolean indexing need not carry ``attrs``.
    complete = bool(ticks.attrs.get("complete", True))
    day = trade_date or ticks["time"].iloc[0].date().isoformat()
    # Post-close prints (15:01+, a handful of 100-share lots at the closing
    # price) are a different market segment. The 15:00:01 closing-auction batch
    # is *not* in this bucket — it is session volume and the daily bar counts it.
    clocks = ticks["time"].dt.strftime("%H:%M:%S")
    post_close = ticks[clocks >= _POST_CLOSE_FIRST]
    ticks = ticks[clocks <= _SESSION_LAST]
    if ticks.empty:
        return {}

    clocks = ticks["time"].dt.strftime("%H:%M:%S")
    open_auction = ticks[clocks < _OPEN_AUCTION_BEFORE]
    close_auction = ticks[(clocks >= "15:00:00") & (clocks <= _SESSION_LAST)]

    volume = float(ticks["volume"].sum())
    amount = float(ticks["amount"].sum())
    by_side = ticks.groupby("side")
    buy_amount = float(by_side["amount"].sum().get("B", 0.0))
    sell_amount = float(by_side["amount"].sum().get("S", 0.0))
    buy_volume = float(by_side["volume"].sum().get("B", 0.0))
    sell_volume = float(by_side["volume"].sum().get("S", 0.0))
    neutral_volume = max(volume - buy_volume - sell_volume, 0.0)

    directional = buy_amount + sell_amount
    big = ticks[ticks["amount"] >= _BIG_TRADE_AMOUNT]
    big_amount = float(big["amount"].sum())
    big_buy = float(big.loc[big["side"] == "B", "amount"].sum())
    big_sell = float(big.loc[big["side"] == "S", "amount"].sum())

    return {
        "symbol": symbol,
        "date": day,
        "trades": int(len(ticks)),
        "volume": volume,
        "amount": amount,
        "vwap": amount / volume if volume else None,
        # Denominator is directional volume only: neutral prints have no
        # initiator, and including them would drag every ratio toward 0.5.
        "active_buy_ratio": buy_amount / directional if directional else None,
        "net_active_amount": buy_amount - sell_amount,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "neutral_volume": neutral_volume,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "big_amount": big_amount,
        # Scale-free companion to big_amount: a fixed notional cut-off means
        # "big" for a 5元 stock and "median" for a 1300元 one.
        "big_amount_ratio": big_amount / amount if amount else None,
        "big_net_amount": big_buy - big_sell,
        "big_trade_count": int(len(big)),
        "avg_trade_amount": amount / len(ticks) if len(ticks) else None,
        "post_close_volume": float(post_close["volume"].sum()),
        "post_close_amount": float(post_close["amount"].sum()),
        # False means pagination was cut short, so this row understates the
        # session. Storing the flag (instead of dropping the row) keeps the
        # gap visible and re-runnable rather than looking like a quiet day.
        "prints_complete": complete,
        "open_auction_volume": float(open_auction["volume"].sum()),
        "open_auction_amount": float(open_auction["amount"].sum()),
        "close_auction_volume": float(close_auction["volume"].sum()),
        "close_auction_amount": float(close_auction["amount"].sum()),
        "first_time": ticks["time"].min().strftime("%H:%M:%S"),
        "last_time": ticks["time"].max().strftime("%H:%M:%S"),
        "source": "tencent_ticks",
    }


def volume_deviation(volume_shares: float, bar_volume_lots: float | None) -> float | None:
    """Percent difference between print volume and the session's bar volume.

    A complete print session reproduces the daily bar volume to ~0.05% (verified
    on 600519.SH: 59,931手 vs 59,927手). Anything beyond a percent means the
    pagination was cut short or the feed served a different session, which is
    exactly the failure that would otherwise look like a quiet trading day.

    Args:
        volume_shares: ``volume`` from :func:`aggregate` (shares).
        bar_volume_lots: Daily bar volume in 手 (Tencent/AKShare convention).

    Returns:
        Deviation in percent, or None when no reference volume is available.
    """
    try:
        reference = float(bar_volume_lots) if bar_volume_lots else 0.0
    except (TypeError, ValueError):
        return None
    if reference <= 0:
        return None
    return (float(volume_shares) / 100.0 / reference - 1.0) * 100.0


def save(rows: Iterable[dict[str, Any]]) -> int:
    """Upsert aggregated rows into the lake (key: symbol + date)."""
    records = [row for row in rows if row]
    if not records:
        return 0
    frame = pd.DataFrame(records)
    for column in _COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[_COLUMNS]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "symbol"])
    if frame.empty:
        return 0
    from src.data.registry import spec as get_spec

    dataset_spec = get_spec(DATASET)
    return lake.write_dataset(
        frame,
        DATASET,
        partition_by=dataset_spec.partitions[0] if dataset_spec.partitions else None,
        key=dataset_spec.keys,
        sort_by=("symbol", "date"),
    )


def load(
    start: str | None = None,
    end: str | None = None,
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read aggregated tick features back from the lake."""
    frame = lake.read_dataset(DATASET)
    if frame.empty:
        return frame
    if "date" in frame.columns:
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if start:
            frame = frame[dates >= pd.Timestamp(start)]
            dates = pd.to_datetime(frame["date"], errors="coerce")
        if end:
            frame = frame[dates <= pd.Timestamp(end)]
    if symbols is not None and "symbol" in frame.columns:
        frame = frame[frame["symbol"].isin(set(symbols))]
    return frame.reset_index(drop=True)


def today_session(now: dt.datetime | None = None) -> str:
    """Beijing calendar date of the session the feed is currently serving."""
    stamp = now or dt.datetime.now(_CN_TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_CN_TZ)
    return stamp.astimezone(_CN_TZ).date().isoformat()


def session_is_complete(now: dt.datetime | None = None) -> bool:
    """Whether the prints currently served are final.

    Only the *current* trading day is at risk of being partial. On a weekend or
    holiday the feed still holds the previous, completed session, so a
    ``now < 15:00`` test alone would wrongly reject a perfectly good Saturday
    collection run.
    """
    stamp = now or dt.datetime.now(_CN_TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_CN_TZ)
    stamp = stamp.astimezone(_CN_TZ)
    try:
        from src.data.calendar import is_trading_day

        if not is_trading_day(stamp.date().isoformat()):
            return True
    except Exception:  # noqa: BLE001 — calendar unavailable: fall back to clock
        pass
    close = stamp.replace(hour=_CLOSE_HOUR, minute=_CLOSE_MINUTE, second=0, microsecond=0)
    return stamp >= close


def coverage() -> pd.DataFrame:
    """Per-symbol coverage of the tick dataset (first/last session, row count)."""
    frame = lake.read_dataset(DATASET, columns=["symbol", "date"])
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "sessions", "first_date", "last_date"])
    dates = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.assign(date=dates).dropna(subset=["date"])
    grouped = frame.groupby("symbol")["date"]
    return pd.DataFrame(
        {
            "symbol": grouped.size().index,
            "sessions": grouped.size().to_numpy(),
            "first_date": grouped.min().to_numpy(),
            "last_date": grouped.max().to_numpy(),
        }
    ).sort_values("symbol", ignore_index=True)


__all__ = [
    "DATASET",
    "aggregate",
    "coverage",
    "fetch_ticks",
    "load",
    "save",
    "session_is_complete",
    "today_session",
    "volume_deviation",
]
