"""Lake-first OHLCV fetching: read the data centre, close only real gaps.

Every screener in this app used to pull its own copy of the market: a 400-day
window of ~4600 names, ~230 batched ``daily`` calls, re-downloaded on every new
session because the loader cache key is content-addressed on (start, end) and
``start`` moves with ``end``. The data centre (``src.data.ingest`` → lake) had
already written those same bars minutes earlier, and nothing read them.

This module is the one place that decides how a scan gets its bars:

* the lake is asked first, offline, for the whole window;
* a stored frame that spans the window is used as-is — zero network;
* a frame that stops before the window end is topped up with only
  ``[last + 1, end]``, when that splice cannot mix price regimes;
* a daily window that reaches Beijing **today** is cut back to the previous
  settled session first. Today's bar is still forming: it is not a lake tail
  gap, it is not fetched from Tushare (or any other daily loader) just to fill
  the calendar day, and it is not written into the lake. Callers that need
  today's price overlay a live quote. ``VIBE_TRADING_DAILY_INCLUDE_FORMING=1``
  restores the old "ask the vendor for today" tail;
* everything else (no stored rows, stored history too short, unknown
  provenance) is pulled from the network as before;
* a confirmed unadjusted Tushare pull is written back into the lake, so the
  next caller — another scan, a backtest, the correlation page — reads the
  cache instead of spending the quota again.

Callers pass their own network fetcher so no import cycle forms and tests can
substitute one. ``VIBE_TRADING_DATAHUB_STRICT`` serves the lake only: a stale
window is returned with an error instead of touching a vendor.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from src.config.accessor import read_env

if TYPE_CHECKING:
    from src.data.hub import StoredBars

logger = logging.getLogger(__name__)

# One refresh at a time, re-entrant so a scan that calls back into this module
# (data-centre read → loader → data-centre read of the gap) cannot deadlock
# itself. A second thread waits, then re-reads the lake the first thread just
# filled instead of issuing the same vendor call.
_refresh_lock = threading.RLock()

# ``(symbols, start, end, source, interval) -> (frames, per-symbol errors)``.
NetworkFetch = Callable[..., tuple[dict[str, pd.DataFrame], dict[str, str]]]

# A-share symbols the daily lake is allowed to store. HK/US/crypto bars from a
# correlation request must not land in ``bars_daily``.
_CN_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")
# The only loader whose daily frames match the lake contract (unadjusted OHLC,
# volume in 手). Tencent and Eastmoney loaders return 前复权 and must not be
# upserted over those rows. Amount on the Tushare loader is 千元; the lake
# stores 元, so the ×1000 happens here and nowhere else.
_CACHEABLE_SOURCE = "tushare"
_OHLC = ("open", "high", "low", "close")

# A stored series may start this many calendar days after the requested window
# and still drive every rule: 400 days are requested for ~250 sessions of
# history, so the head is padding, not evidence. Wider than that means the lake
# genuinely lacks the lookback and the name needs a full pull.
LAKE_HEAD_GRACE_DAYS = 7
# Below this many names a tail gap is cheaper to fetch than to probe for: the
# probe is one extra index call, so it only pays off on a wide bucket.
PROBE_MIN_SYMBOLS = 50
# Sources that cannot be asked for over the network. A lake row tagged with one
# of these has no matching loader, so its tail must come from somewhere else.
_NON_NETWORK_SOURCES = frozenset({"", "lake", "local", "auto"})
# A stored series that stops this many days before the newest series in the same
# read is not lagging ingestion, it is a suspended or delisted name: asking the
# vendor for its "missing" tail returns nothing but costs one call per name, and
# in a full-market read that is hundreds of single-symbol round-trips — exactly
# the pattern the batched loader exists to avoid. The window is measured against
# the market's own latest bar, so a lake that is entirely stale (ingestion down)
# skips nobody and the whole universe is topped up as usual.
STALE_TAIL_DAYS = 30

BarsSource = str | None


@dataclass
class FetchStats:
    """Where one scan's bars actually came from.

    ``label()`` is what the scan stamps on its result so the UI can say whether
    a screen was served by the data centre or had to go out to a vendor.
    """

    requested: int = 0
    lake_bases: int = 0
    tail_spliced: int = 0
    pulled_whole: int = 0
    unavailable: int = 0
    stale_kept: int = 0
    # Bar pulls actually sent to a loader (a holiday probe is not one: it answers
    # a calendar question and returns no bars).
    network_calls: int = 0
    probes: int = 0
    probe_skipped: bool = False

    def label(self) -> BarsSource:
        """``lake`` / ``lake+network`` / ``network``; ``None`` when unknown.

        ``lake`` means not one bar came off the network — a scan where a handful
        of names still needed a full pull is ``lake+network``, because saying
        "data centre" there would overstate what the stored data covered.

        ``None`` is the honest answer for a scan whose fetcher was injected
        (tests, replays) — nothing here observed a lake read.
        """
        if self.requested <= 0:
            return None
        if self.network_calls == 0:
            return "lake"
        if self.pulled_whole >= self.requested:
            return "network"
        return "lake+network"

    def log_line(self, label: str) -> str:
        probes = f", {self.probes} probe(s)" if self.probes else ""
        return (
            f"{label} bars: {self.lake_bases} lake bases "
            f"({self.tail_spliced} tail-spliced, {self.stale_kept} stale kept), "
            f"{self.pulled_whole} pulled whole, {self.unavailable} unavailable"
            f"{probes} → {self.label() or 'unknown'}"
        )


def settled_daily_end(
    end: str,
    *,
    interval: str = "1D",
    today: dt.date | None = None,
) -> str:
    """Inclusive end that a daily loader may be asked for.

    A request whose end is Beijing today (or later) is cut back to the previous
    settled session. The lake stores settled bars only; a missing today is not
    a gap to close with ``daily()``. Historical ends (strictly before Beijing
    today) are returned unchanged.

    ``today`` is injectable so tests can freeze the Beijing clock. Non-daily
    intervals are not capped. ``VIBE_TRADING_DAILY_INCLUDE_FORMING=1`` disables
    the cap.
    """
    if not _is_daily_interval(interval) or _include_forming_daily():
        return end
    try:
        end_day = pd.Timestamp(end).date()
    except (TypeError, ValueError):
        return end
    clock = today or _beijing_today()
    if end_day < clock:
        return end
    return _previous_settled_session(clock).isoformat()


def _is_daily_interval(interval: str) -> bool:
    return str(interval or "").strip().lower() in {"1d", "d", "day", "daily"}


def _include_forming_daily() -> bool:
    """True when the operator explicitly wants a vendor pull of today's bar.

    The bar is still not written into the lake (see ``_prepare_tushare_frame``).
    An explicit env value wins over the cached config singleton, matching
    :func:`src.data.hub.strict_mode`.
    """
    raw = read_env("VIBE_TRADING_DAILY_INCLUDE_FORMING").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        from src.config.accessor import get_env_config

        return bool(get_env_config().data.vibe_trading_daily_include_forming)
    except Exception:  # noqa: BLE001 — default is settled bars only
        return False


def _previous_settled_session(today: dt.date) -> dt.date:
    """Last exchange session strictly before *today*, without a calendar refresh.

    A seeded calendar that already reaches *today* wins (so a holiday weekday
    is not treated as T-1). A missing or stale seed falls back to the previous
    weekday — the same approximation market-pick uses — and a remaining holiday
    gap is still answered by the index probe when the lake is actually behind.
    """
    try:
        from src.data.calendar import cached_previous_trading_day

        found = cached_previous_trading_day(today)
        if found:
            return dt.date.fromisoformat(found)
    except Exception:  # noqa: BLE001 — weekday fallback, never a vendor call
        logger.debug("settled-session calendar lookup failed", exc_info=True)
    settled = today - dt.timedelta(days=1)
    while settled.weekday() >= 5:
        settled -= dt.timedelta(days=1)
    return settled


def ensure_bars(
    symbols: list[str],
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
    network: NetworkFetch,
    label: str = "data-center",
    stats: FetchStats | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], FetchStats]:
    """Read K-lines from the data centre, refreshing and storing them when stale.

    This is the call features should make. A fresh lake window is returned
    with no vendor traffic. A gap is fetched through ``network`` (the existing
    loader), written back when the bars are safe to store, and then returned.
    The next caller of the same window hits the lake.

    ``network`` stays injectable so a test can answer the gap without a vendor
    and so the data centre does not import the backtest runner.
    """
    return fetch_lake_first(
        symbols,
        start=start,
        end=end,
        source=source,
        interval=interval,
        network=network,
        label=label,
        stats=stats,
    )


def fetch_lake_first(
    symbols: list[str],
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
    network: NetworkFetch,
    label: str = "scan",
    stats: FetchStats | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], FetchStats]:
    """Serve ``symbols`` from the lake, closing only the gaps from the network.

    Holds the refresh lock for the whole read-or-fetch so two features asking
    for the same stale window do not both pay the vendor.
    """
    with _refresh_lock:
        return _fetch_lake_first_locked(
            symbols,
            start=start,
            end=end,
            source=source,
            interval=interval,
            network=network,
            label=label,
            stats=stats,
        )


def _fetch_lake_first_locked(
    symbols: list[str],
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
    network: NetworkFetch,
    label: str = "scan",
    stats: FetchStats | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, str], FetchStats]:
    """Unlocked body of :func:`fetch_lake_first`. Do not call this directly.

    Args:
        symbols: Universe to return, in any order.
        start: Inclusive window start (``YYYY-MM-DD``).
        end: Inclusive window end.
        source: Loader the caller would use for the network part. ``auto`` is
            resolved per symbol to whatever source wrote that lake frame, so a
            splice never joins two adjustment regimes.
        interval: Bar size, e.g. ``1D``.
        network: Network fetcher, called only for names the lake cannot serve.
        label: Log prefix, e.g. ``market-pick``.
        stats: Optional collector; a fresh one is used when omitted.

    Returns:
        ``(frames, per-symbol errors, stats)``. A name whose stored frame is
        current is never reported as an error — a suspended stock legitimately
        has no bar for the latest session.
    """
    stats = stats if stats is not None else FetchStats()
    stats.requested = len(symbols)
    if not symbols:
        return {}, {}, stats

    requested_end = end
    end = settled_daily_end(end, interval=interval)
    if end != requested_end:
        logger.info(
            "%s: daily end %s is Beijing's forming session; "
            "using settled bars through %s "
            "(live quote overlay, not a daily vendor refresh)",
            label,
            requested_end,
            end,
        )
    try:
        if pd.Timestamp(start).date() > pd.Timestamp(end).date():
            # The caller asked only for the session that is still forming.
            logger.info(
                "%s: window %s..%s is only the forming session; "
                "not refreshing daily bars",
                label,
                start,
                requested_end,
            )
            return {}, {}, stats
    except (TypeError, ValueError):
        pass

    stored = read_lake(symbols, start=start, end=end, interval=interval)
    if not stored.frames:
        if _strict():
            logger.warning(
                "%s: data center strict mode, not refreshing %d symbol(s) %s..%s",
                label,
                len(symbols),
                start,
                end,
            )
            errors = {
                symbol: "data center strict mode: refresh skipped" for symbol in symbols
            }
            stats.unavailable = len(symbols)
            logger.info(stats.log_line(label))
            return {}, errors, stats
        data_map, errors = _pull(
            network,
            symbols,
            start=start,
            end=end,
            source=source,
            interval=interval,
            stats=stats,
        )
        stats.pulled_whole = len(symbols)
        stats.unavailable = len([s for s in symbols if s not in data_map])
        _cache_refresh(data_map, source=source, interval=interval, label=label)
        logger.info(stats.log_line(label))
        return data_map, errors, stats

    reuse, tails, refresh, stale = _split_coverage(
        symbols, stored, start=start, end=end, source=source
    )
    data_map: dict[str, pd.DataFrame] = dict(reuse)
    fetch_errors: dict[str, str] = {}
    stats.lake_bases = len(reuse)
    stats.pulled_whole = len(refresh)
    stats.stale_kept = len(stale)

    if _strict() and (refresh or tails):
        logger.warning(
            "%s: data center strict mode, keeping lake frames for %s..%s "
            "(%d name(s) not refreshed)",
            label,
            start,
            end,
            len(refresh) + sum(len(bucket) for bucket in tails.values()),
        )
        for symbol in refresh:
            fetch_errors[symbol] = "data center strict mode: refresh skipped"
        stats.pulled_whole = 0
        stats.unavailable = len(refresh)
        logger.info(stats.log_line(label))
        return data_map, fetch_errors, stats

    if refresh:
        fetched, errors = _pull(
            network,
            refresh,
            start=start,
            end=end,
            source=source,
            interval=interval,
            stats=stats,
        )
        data_map.update(fetched)
        fetch_errors.update(errors)
        _cache_refresh(fetched, source=source, interval=interval, label=label)

    for (gap_start, gap_end, tail_source), bucket in tails.items():
        if not bucket:
            continue
        # One probe call only pays for itself on a wide gap; a handful of names
        # is cheaper to just ask for.
        if len(bucket) >= PROBE_MIN_SYMBOLS and not _market_traded_in_gap(
            gap_start, gap_end, source=tail_source, interval=interval,
            network=network, stats=stats,
        ):
            stats.probe_skipped = True
            logger.debug(
                "%s: no session in %s..%s; lake frames already current",
                label,
                gap_start,
                gap_end,
            )
            continue
        fetched, errors = _pull(
            network,
            bucket,
            start=gap_start,
            end=gap_end,
            source=tail_source,
            interval=interval,
            stats=stats,
        )
        fresh: dict[str, pd.DataFrame] = {}
        for symbol in bucket:
            tail = fetched.get(symbol)
            if tail is not None and not tail.empty:
                data_map[symbol] = _splice(data_map[symbol], tail)
                fresh[symbol] = tail
                stats.tail_spliced += 1
                continue
            # Nothing new for this name: a suspension, or the market had no
            # session in the gap. The lake frame is as current as it can be.
            reason = errors.get(symbol) or ""
            if "no OHLCV bars returned" in reason:
                logger.debug(
                    "%s: %s has no bar in %s..%s; keeping lake frame",
                    label,
                    symbol,
                    gap_start,
                    gap_end,
                )
            else:
                fetch_errors[symbol] = reason or "tail gap fetch returned nothing"
        _cache_refresh(fresh, source=tail_source, interval=interval, label=label)

    missing = [symbol for symbol in symbols if symbol not in data_map]
    for symbol in missing:
        fetch_errors.setdefault(symbol, "no OHLCV bars returned")
    stats.unavailable = len(missing)

    logger.info(stats.log_line(label))
    return data_map, fetch_errors, stats


def _pull(
    network: NetworkFetch,
    symbols: list[str] | tuple[str, ...],
    *,
    start: str,
    end: str,
    source: str,
    interval: str,
    stats: FetchStats,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Call the loader, turning a hard failure into per-symbol errors.

    The stored frames are already in hand when this runs, so an exception here
    must not abort a screen that is 99% local: the caller keeps what the lake
    gave it and reports the names that could not be refreshed. Before, the pull
    was the only source, so letting the error through was equivalent.
    """
    if not symbols:
        return {}, {}
    stats.network_calls += 1
    try:
        return network(
            list(symbols), start=start, end=end, source=source, interval=interval
        )
    except Exception as exc:  # noqa: BLE001 — keep the lake frames, report the gap
        logger.warning(
            "network fetch failed for %d name(s) in %s..%s: %s",
            len(symbols),
            start,
            end,
            exc,
        )
        return {}, {symbol: f"OHLCV fetch failed: {exc}" for symbol in symbols}


def read_lake(
    symbols: list[str], *, start: str, end: str, interval: str
) -> StoredBars:
    """Read the lake through the hub; never raises and never hits the network.

    Dirty rows are removed by the hub itself now (one vectorised ``validate_ohlc``
    pass over the long frame), so the frames handed back are already clean and
    cost nothing extra per symbol here.
    """
    from src.data.hub import StoredBars

    try:
        from src.data import hub

        return hub.bars_frame(interval, symbols=list(symbols), start=start, end=end)
    except Exception as exc:  # noqa: BLE001 — fall back to a network pull
        logger.warning("lake read failed, using the network: %s", exc)
        return StoredBars(interval=interval, frames={}, sources={}, derived_from=None)


def _split_coverage(
    symbols: list[str],
    stored: StoredBars,
    *,
    start: str,
    end: str,
    source: str,
) -> tuple[
    dict[str, pd.DataFrame],
    dict[tuple[str, str, str], list[str]],
    list[str],
    list[str],
]:
    """Sort the universe into lake-served, tail-spliced and network-only names.

    The head grace exists because the window is deliberately padded (400 calendar
    days for roughly 250 sessions of history): a stored series starting a few
    days late still carries every session the rules actually read, and asking
    again for the missing head would cost a full re-download.

    Tails are bucketed by ``(gap_start, gap_end, tail_source)`` rather than by
    date alone: with ``source="auto"`` each name follows the source that wrote
    its own lake frame, so one scan can need two different vendors.

    Names whose stored series ends long before the market's own latest session
    are returned as stale (kept, never fetched). Their gap is not missing data —
    the name stopped trading — and each one would otherwise be its own bucket and
    its own single-symbol round-trip.
    """
    head_limit = pd.Timestamp(start) + pd.Timedelta(days=LAKE_HEAD_GRACE_DAYS)
    end_stamp = pd.Timestamp(end)
    market_latest = _market_latest(stored)
    stale_cutoff = (
        market_latest - pd.Timedelta(days=STALE_TAIL_DAYS)
        if market_latest is not None
        else None
    )
    reuse: dict[str, pd.DataFrame] = {}
    tails: dict[tuple[str, str, str], list[str]] = {}
    refresh: list[str] = []
    stale: list[str] = []

    for symbol in symbols:
        frame = stored.frames.get(symbol)
        if frame is None or frame.empty:
            refresh.append(symbol)
            continue
        if frame.index.min() > head_limit:
            # Stored history is too short for the lookback: classify on a full pull.
            refresh.append(symbol)
            continue
        last_bar = frame.index.max()
        if last_bar >= end_stamp:
            reuse[symbol] = frame
            continue
        if stale_cutoff is not None and last_bar < stale_cutoff:
            # Suspended or delisted: the vendor has nothing newer to give.
            reuse[symbol] = frame
            stale.append(symbol)
            continue
        tail_source = _tail_source(stored.sources.get(symbol, ""), source)
        if tail_source is None:
            refresh.append(symbol)
            continue
        # Keep the stored frame as the base and record that it still needs a tail.
        reuse[symbol] = frame
        gap_start = (last_bar + pd.Timedelta(days=1)).date().isoformat()
        tails.setdefault((gap_start, end, tail_source), []).append(symbol)

    return reuse, tails, refresh, stale


def _market_latest(stored: StoredBars) -> pd.Timestamp | None:
    """The session the lake considers current, taken as the modal last bar.

    The newest bar overall would be a single name with a bad future timestamp;
    the most common last-bar date is what ingestion actually delivered for the
    market. When the whole lake is behind, this is behind too, so no name looks
    stale and every tail is fetched — the correct reading of "ingestion lagging".
    """
    stamps = [frame.index.max() for frame in stored.frames.values() if not frame.empty]
    if not stamps:
        return None
    counts = pd.Series(stamps).value_counts()
    tied = counts[counts == counts.max()].index
    return max(tied)


def _tail_source(lake_source: str, network_source: str) -> str | None:
    """Which loader may top up a frame written by *lake_source*, if any.

    Only a matching source is trusted: a lake written by ``tencent`` and a tail
    from ``tushare`` may disagree on adjustment, which shows up as one bogus
    return at the seam and a wrong 20-day momentum for that name.

    ``auto`` is resolved to the lake's own source instead of refusing the
    splice. Auto would route the tail to whatever loader answers first — for
    A-shares usually a different vendor than the one that filled the lake — so
    following the lake is both the only safe splice and the only way an
    auto-configured scan gets any benefit from stored bars.
    """
    lake = (lake_source or "").strip().lower()
    requested = (network_source or "").strip().lower()
    if lake in _NON_NETWORK_SOURCES:
        return None
    if not requested or requested == "auto":
        return lake
    return lake if lake == requested else None


def _market_traded_in_gap(
    gap_start: str,
    gap_end: str,
    *,
    source: str,
    interval: str,
    network: NetworkFetch,
    stats: FetchStats | None = None,
) -> bool:
    """Ask one index whether the market traded in the gap.

    ``_settled_end`` only skips weekends, so on a public holiday it hands back a
    weekday with no session. Without this probe every name would be asked for a
    bar that cannot exist — hundreds of calls returning nothing. The benchmark
    index trades every session, so one request answers the question for the
    whole market.

    Returns ``True`` unless the benchmark came back *successfully empty*. A probe
    that failed for another reason (bad token, vendor 500) must not be read as
    "holiday", or a whole stale universe would be silently declared current.
    """
    from src.monitor.trend_regime import CSI300

    probe_stats = stats if stats is not None else FetchStats()
    probe_stats.probes += 1
    try:
        probe, errors = network(
            [CSI300], start=gap_start, end=gap_end, source=source, interval=interval
        )
    except Exception as exc:  # noqa: BLE001 — fail open: try to close the gap
        logger.debug("session probe failed (%s); fetching the gap", exc)
        return True
    frame = probe.get(CSI300)
    if frame is not None and not frame.empty:
        return True
    reason = errors.get(CSI300) or ""
    if reason and "no OHLCV bars returned" not in reason:
        logger.debug("session probe unavailable (%s); fetching the gap", reason)
        return True
    return False


def _splice(frame: pd.DataFrame, tail: pd.DataFrame) -> pd.DataFrame:
    """Append freshly fetched bars to a stored frame, network winning on overlap."""
    merged = pd.concat([frame, tail])
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


def _strict() -> bool:
    """True when the data centre must not read through to a vendor."""
    try:
        from src.data.hub import strict_mode

        return strict_mode()
    except Exception:  # noqa: BLE001 — a config failure must not force a vendor call
        logger.warning("data center strict-mode check failed; refusing the refresh")
        return True


def _beijing_today() -> dt.date:
    """Calendar date in Beijing, used to keep a forming session out of the lake."""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _confirmed_source(frame: pd.DataFrame, requested: str) -> str:
    """Which vendor actually produced ``frame``, if the caller said so.

    The loader adapter stamps ``data_center_source`` after it knows whether a
    fallback ran. An injected fetcher (tests, a direct ``tushare`` pull) does
    not, and the requested source is then the truth. ``skip`` means "do not
    write this" — a forward-adjusted fallback must not overwrite the lake.
    """
    stamped = ""
    if isinstance(getattr(frame, "attrs", None), dict):
        stamped = str(frame.attrs.get("data_center_source") or "").strip().lower()
    if stamped:
        return stamped
    return (requested or "").strip().lower()


def _cache_refresh(
    frames: dict[str, pd.DataFrame],
    *,
    source: str,
    interval: str,
    label: str,
) -> None:
    """Write a confirmed unadjusted refresh into the lake. Failures are logged.

    The scan already holds the frames it will return, so a disk error here must
    not blank the screen. The next caller simply refreshes again.
    """
    if not frames:
        return
    token = str(interval or "").strip().lower()
    if token not in {"1d", "d", "day", "daily"}:
        logger.info(
            "%s: not caching %s bars (the daily lake is the shared cache)",
            label,
            interval,
        )
        return

    cacheable: dict[str, pd.DataFrame] = {}
    for symbol, frame in frames.items():
        if not _CN_SYMBOL.fullmatch(str(symbol).strip().upper()):
            continue
        if _confirmed_source(frame, source) != _CACHEABLE_SOURCE:
            continue
        prepared = _prepare_tushare_frame(frame)
        if prepared is not None:
            cacheable[str(symbol).strip().upper()] = prepared
    if not cacheable:
        logger.info(
            "%s: %d refreshed series not cached "
            "(only settled unadjusted tushare A-share bars are written to the lake)",
            label,
            len(frames),
        )
        return
    try:
        from src.data.bars import save_bars

        rows = save_bars(cacheable, "1d", source=_CACHEABLE_SOURCE)
    except Exception as exc:  # noqa: BLE001 — serve the bars we already have
        logger.warning("%s: failed to cache refreshed bars: %s", label, exc)
        return
    logger.info(
        "%s: cached %d %s row(s) for %d symbol(s)",
        label,
        rows,
        _CACHEABLE_SOURCE,
        len(cacheable),
    )


def _prepare_tushare_frame(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Lake-shaped copy of one Tushare loader frame, settled sessions only.

    Today's bar is still forming, so it is served to the caller but not stored.
    Amount is scaled from 千元 to 元. A null amount does not wipe a factor or a
    turnover the ingester already wrote — :func:`save_bars` keeps that rule.
    """
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return None
    work = frame.copy()
    index = pd.to_datetime(work.index, errors="coerce")
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    work.index = index
    work = work[work.index.notna()]
    cutoff = pd.Timestamp(_beijing_today())
    work = work[work.index.normalize() < cutoff]
    if work.empty:
        return None
    if any(column not in work.columns for column in _OHLC):
        return None
    out = pd.DataFrame(
        {column: pd.to_numeric(work[column], errors="coerce") for column in _OHLC},
        index=work.index,
    )
    if "volume" in work.columns:
        out["volume"] = pd.to_numeric(work["volume"], errors="coerce")
    if "amount" in work.columns:
        out["amount"] = pd.to_numeric(work["amount"], errors="coerce") * 1000.0
    out = out.dropna(subset=list(_OHLC))
    if out.empty:
        return None
    out.index.name = "date"
    return out
