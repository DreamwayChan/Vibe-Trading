"""Hub: the single read façade over the lake.

Every consumer — pages, agent tools, backtest loaders — is meant to ask the hub
instead of reaching for a loader, so that "which session", "settled or not",
"which source", "how complete" are answered once, in one place, and returned
with the data rather than inferred per screen.

Phase 0 exposes the console's read surface: as-of resolution, per-dataset
health, lake totals and the aggregate overview.  Series reads go through
:func:`bars` / :func:`bars_frame`.  :func:`ensure_bars` is the shared cache
for features that need a window which might be stale: serve the lake when it
covers the request, otherwise refresh, store, and serve.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Sequence

import pandas as pd

from src.config.accessor import read_env
from src.data import catalog
from src.data.registry import REGISTRY, DatasetSpec, spec as get_spec

logger = logging.getLogger(__name__)

__all__ = [
    "AsOf",
    "CN_TZ",
    "CLOSE_TIME",
    "SETTLE_TIME",
    "now",
    "resolve_as_of",
    "strict_mode",
    "lake_stats",
    "dataset_health",
    "dataset_cards",
    "dataset_detail",
    "overview",
    "quality_alerts",
    "quota_status",
    "manifest_for",
    "bars",
    "bars_frame",
    "ensure_bars",
    "StoredBars",
    "coverage_grid",
    "BAR_POINT_LIMIT",
]

# China has no DST, so a fixed offset avoids a tzdata dependency on Windows.
CN_TZ = dt.timezone(dt.timedelta(hours=8))
CLOSE_TIME = dt.time(15, 0)
SETTLE_TIME = dt.time(15, 30)
# Auto-rebuild is a convenience for a console opened on a fresh lake, not a
# background job: refuse to scan a dataset that has already grown large.
_AUTO_REBUILD_FILE_LIMIT = 500

_STRICT_ENV = "VIBE_TRADING_DATAHUB_STRICT"


@dataclass(frozen=True)
class AsOf:
    """The session an answer is about, and whether it can still change."""

    date: str
    settled: bool
    reason: str
    live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "settled": self.settled,
            "live": self.live,
            "reason": self.reason,
        }


def now() -> dt.datetime:
    """Current wall clock in Beijing time."""
    return dt.datetime.now(tz=CN_TZ)


def strict_mode() -> bool:
    """True when reads must never read through to the network."""
    raw = read_env(_STRICT_ENV).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    try:
        from src.config.accessor import get_env_config

        return bool(getattr(get_env_config().data, "vibe_trading_datahub_strict", False))
    except Exception:  # noqa: BLE001 - config import is best effort
        return False


def resolve_as_of(live: bool = False, at: dt.datetime | None = None) -> AsOf:
    """Resolve the default session for reads.

    The console and every downstream page must agree on one answer, so the rule
    lives here rather than in each route:

    * after 15:30 on a session -> that session, settled;
    * during a session, ``live=True`` -> today, not settled (bars still forming);
    * during a session, default -> the previous session, settled;
    * a holiday/weekend -> the previous session, settled.
    """
    from src.data import calendar

    moment = at or now()
    today = moment.date()

    if not calendar.is_trading_day(today):
        prev = calendar.previous_trading_day(today)
        return AsOf(prev, True, "非交易日，取上一个交易日")

    if moment.time() >= SETTLE_TIME:
        return AsOf(today.isoformat(), True, "已收盘结算")

    if live:
        return AsOf(today.isoformat(), False, "盘中，数据仍在变化", live=True)

    prev = calendar.previous_trading_day(today)
    return AsOf(prev, True, "今日未结算，取上一交易日")


def manifest_for(name: str, *, refresh: bool = False) -> dict[str, Any] | None:
    """Return a dataset manifest, rebuilding it when missing and cheap to scan."""
    if refresh:
        return catalog.rebuild(name)
    stored = catalog.read_manifest(name)
    if stored is not None:
        return stored
    files = catalog.file_count(name)
    if files == 0:
        return None
    if files > _AUTO_REBUILD_FILE_LIMIT:
        return {
            "dataset": name,
            "files": files,
            "rows": None,
            "note": "manifest missing; rebuild from the datasets tab to scan it",
        }
    return catalog.rebuild(name)


def lake_stats() -> dict[str, Any]:
    """Total size and file count of the lake (catalog bookkeeping excluded)."""
    from src.data.lake import lake_root

    root = lake_root()
    files = 0
    size = 0
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix != ".parquet":
                continue
            files += 1
            try:
                size += path.stat().st_size
            except OSError:
                continue
    return {"root": str(root), "files": files, "bytes": size, "exists": root.exists()}


def _lag_sessions(date_max: str | None, as_of: str) -> int | None:
    """Number of sessions strictly after *date_max* up to *as_of*."""
    if not date_max:
        return None
    from src.data import calendar

    if date_max >= as_of:
        return 0
    start = (dt.date.fromisoformat(date_max) + dt.timedelta(days=1)).isoformat()
    try:
        return len(calendar.trading_days(start, as_of))
    except Exception:  # noqa: BLE001 - calendar is best effort here
        return None


def dataset_health(
    dataset_spec: DatasetSpec,
    *,
    as_of: AsOf | None = None,
    moment: dt.datetime | None = None,
) -> dict[str, Any]:
    """Freshness of one dataset: ``ok`` / ``pending`` / ``lag`` / ``stale`` / ``empty``.

    ``pending`` exists so a console opened at 15:10 does not paint yesterday's
    good data red before the ingest SLA has even elapsed.
    """
    target = as_of or resolve_as_of()
    clock = moment or now()
    manifest = manifest_for(dataset_spec.name)

    rows = None
    date_max = None
    if manifest:
        rows = manifest.get("rows")
        date_max = manifest.get("date_max")

    if not manifest or not rows:
        status = "empty"
        lag = None
    else:
        lag = _lag_sessions(date_max, target.date)
        if lag is None:
            status = "unknown"
        elif lag <= 0:
            status = "ok"
        elif _within_sla(clock, target, dataset_spec.sla_minutes):
            status = "pending"
        elif lag == 1:
            status = "lag"
        else:
            status = "stale"

    return {
        "name": dataset_spec.name,
        "label": dataset_spec.label,
        "category": dataset_spec.category,
        "status": status,
        "rows": rows,
        "symbols": (manifest or {}).get("symbols"),
        "date_min": (manifest or {}).get("date_min"),
        "date_max": date_max,
        "lag_days": lag,
        "files": (manifest or {}).get("files", catalog.file_count(dataset_spec.name)),
        "bytes": (manifest or {}).get("bytes", 0),
        "last_write": (manifest or {}).get("last_write") or (manifest or {}).get("generated_at"),
        "last_source": (manifest or {}).get("last_source"),
        "consumers": list(dataset_spec.consumers),
        "retention_days": dataset_spec.retention_days,
        "sla_minutes": dataset_spec.sla_minutes,
        "dirty": bool((manifest or {}).get("dirty")),
    }


def _within_sla(clock: dt.datetime, as_of: AsOf, sla_minutes: int) -> bool:
    """True when *as_of* is today and the ingest window has not closed yet."""
    if not as_of.settled:
        return False
    try:
        target_day = dt.date.fromisoformat(as_of.date)
    except ValueError:  # pragma: no cover - defensive
        return False
    if target_day != clock.date():
        return False
    deadline = dt.datetime.combine(clock.date(), CLOSE_TIME, tzinfo=CN_TZ) + dt.timedelta(
        minutes=sla_minutes
    )
    return clock < deadline


def dataset_cards(as_of: AsOf | None = None) -> list[dict[str, Any]]:
    """Spec + health for every declared dataset (console table rows)."""
    target = as_of or resolve_as_of()
    cards = []
    for dataset_spec in REGISTRY.values():
        card = dataset_spec.to_dict()
        card["health"] = dataset_health(dataset_spec, as_of=target)
        cards.append(card)
    return cards


def dataset_detail(name: str, *, refresh: bool = False) -> dict[str, Any]:
    """Everything the dataset drawer shows: contract, manifest, quality."""
    dataset_spec = get_spec(name)
    manifest = manifest_for(name, refresh=refresh)
    return {
        "spec": dataset_spec.to_dict(),
        "manifest": manifest,
        "health": dataset_health(dataset_spec),
        "partitions": (manifest or {}).get("partitions", []),
        "quality": (manifest or {}).get("quality", {}),
        "schema_drift": (manifest or {}).get("schema_drift", {"missing": [], "extra": []}),
    }


def quality_alerts(as_of: AsOf | None = None) -> list[dict[str, Any]]:
    """Flattened, severity-tagged issues for the overview pane."""
    target = as_of or resolve_as_of()
    alerts: list[dict[str, Any]] = []

    for dataset_spec in REGISTRY.values():
        health = dataset_health(dataset_spec, as_of=target)
        name = dataset_spec.name
        manifest = catalog.read_manifest(name) or {}

        if health["status"] == "empty":
            alerts.append(
                {
                    "dataset": name,
                    "label": dataset_spec.label,
                    "severity": "info",
                    "code": "empty",
                    "message": f"{dataset_spec.label} 尚无数据",
                }
            )
            continue
        if health["status"] in {"stale", "lag"}:
            alerts.append(
                {
                    "dataset": name,
                    "label": dataset_spec.label,
                    "severity": "warning",
                    "code": health["status"],
                    "message": f"{dataset_spec.label} 落后 {health['lag_days']} 个交易日"
                    f"（最新 {health['date_max']}）",
                }
            )

        quality = manifest.get("quality") or {}
        dup = quality.get("dup_keys")
        if dup:
            alerts.append(
                {
                    "dataset": name,
                    "label": dataset_spec.label,
                    "severity": "warning",
                    "code": "dup_keys",
                    "message": f"{dataset_spec.label} 存在 {dup} 组重复主键",
                }
            )
        for check in quality.get("checks") or []:
            violations = check.get("violations")
            if violations:
                alerts.append(
                    {
                        "dataset": name,
                        "label": dataset_spec.label,
                        "severity": "warning",
                        "code": check.get("id", "check"),
                        "message": f"{dataset_spec.label} 有 {violations} 行未通过 {check.get('id')}",
                    }
                )

        drift = manifest.get("schema_drift") or {}
        missing = drift.get("missing") or []
        if missing:
            alerts.append(
                {
                    "dataset": name,
                    "label": dataset_spec.label,
                    "severity": "info",
                    "code": "schema_drift",
                    "message": f"{dataset_spec.label} 缺列：{', '.join(missing[:5])}",
                }
            )

    severity_rank = {"warning": 0, "info": 1}
    alerts.sort(key=lambda a: severity_rank.get(a["severity"], 9))
    return alerts


# A single symbol's series is capped so an accidental "give me five years of
# 1-minute bars" cannot return a 500 MB payload.  The cap is reported back as
# ``truncated`` rather than silently applied.
BAR_POINT_LIMIT = 20_000

# Bars per symbol are bounded below; this bounds the number of *series* when the
# caller asks for "everything".  5,000 × 1,200 points is not a chart.
MAX_SERIES = 50

# When a coarse intraday interval was never stored, aggregate it from a finer
# one here — once, in the hub — instead of letting every screen resample on its
# own and disagree about bucket boundaries.  Daily is deliberately absent: a
# missing daily dataset must stay visible, not be papered over with 5m bars.
_INTRADAY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "15m": ("5m",),
    "30m": ("15m", "5m"),
    "1H": ("30m", "15m", "5m"),
    "4H": ("1H", "30m", "15m", "5m"),
}


def _read_bars_with_fallback(
    interval: str,
    *,
    symbols: Sequence[str] | None,
    start: str | None,
    end: str | None,
) -> tuple[pd.DataFrame, str | None]:
    """Load bars for *interval*, aggregating from a finer one when needed.

    Returns the frame plus the interval it was derived from (``None`` when the
    data was stored at the requested granularity).
    """
    from src.data import bars as bar_store

    frame = bar_store.load_bars(interval, symbols=symbols, start=start, end=end)
    if not frame.empty:
        return frame, None

    from backtest.loaders._intervals import normalize_interval

    for finer in _INTRADAY_FALLBACKS.get(normalize_interval(interval), ()):
        base = bar_store.load_bars(finer, symbols=symbols, start=start, end=end)
        if base.empty:
            continue
        return _resample_frame(base, interval), finer
    return frame, None


def _resample_frame(frame: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Aggregate a long bar frame per symbol, keeping the long format."""
    from backtest.loaders._intervals import resample_ohlcv
    from src.data import bars as bar_store

    parts = []
    for symbol, per_symbol in bar_store.bars_by_symbol(frame).items():
        aggregated = resample_ohlcv(per_symbol, interval, symbol=symbol)
        if aggregated.empty:
            continue
        part = aggregated.reset_index()
        part = part.rename(columns={part.columns[0]: "date"})
        part["symbol"] = symbol
        part["interval"] = interval
        if "source" in frame.columns:
            part["source"] = frame.loc[frame["symbol"] == symbol, "source"].iloc[0]
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


@dataclass(frozen=True)
class StoredBars:
    """Lake bars for one window, split per symbol, with provenance.

    ``frames`` are the raw OHLCV frames indexed by session — no rounding and no
    JSON reshaping — because the consumer is a screener computing moving
    averages, not a chart. ``sources`` records which source wrote each symbol's
    most recent stored bar, so a caller can tell whether topping the frame up
    from a particular network source would splice two price regimes together.
    """

    interval: str
    frames: dict[str, pd.DataFrame]
    sources: dict[str, str]
    derived_from: str | None

    def __bool__(self) -> bool:
        """True when the lake returned at least one series."""
        return bool(self.frames)


def bars_frame(
    interval: str = "1d",
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> StoredBars:
    """Read lake bars as per-symbol OHLCV frames, offline.

    :func:`bars` is the console's envelope: it rounds prices to two decimals and
    reshapes them into JSON points, which is right for a chart and wrong for a
    signal. Screeners and monitors want the stored frame itself, plus the
    provenance needed to decide whether it can stand in for a network pull.

    Args:
        interval: Bar size, in any spelling ``normalize_interval`` accepts.
        symbols: Symbol whitelist; ``None`` reads every stored symbol.
        start: Inclusive lower bound (``YYYY-MM-DD``).
        end: Inclusive upper bound (``YYYY-MM-DD``).

    Returns:
        :class:`StoredBars`; ``frames`` is empty when nothing is stored. A
        missing or corrupt lake yields an empty result rather than an error, and
        nothing here touches the network.

    Rows that violate OHLC invariants are dropped before splitting (the loader
    boundary check, applied once to the long frame). A screener therefore never
    sees a bar with ``high < low``, which would surface as a NaN metric far from
    its cause.
    """
    from src.data import bars as bar_store

    token = interval
    try:
        from backtest.loaders._intervals import normalize_interval

        token = normalize_interval(interval)
    except Exception:  # noqa: BLE001 — let load_bars judge an exotic token
        token = interval

    try:
        frame, derived_from = _read_bars_with_fallback(
            token, symbols=symbols, start=start, end=end
        )
    except Exception as exc:  # noqa: BLE001 — a corrupt lake must not break a screen
        logger.warning("hub.bars_frame: lake read failed for %s: %s", token, exc)
        return StoredBars(interval=token, frames={}, sources={}, derived_from=None)

    if frame is None or frame.empty:
        return StoredBars(interval=token, frames={}, sources={}, derived_from=None)

    # Lake rows bypass the loader boundary where validate_ohlc normally runs, so
    # the invariant check happens here — once, on the whole long frame, rather
    # than once per symbol downstream. The rules are row-level (high < low, a
    # non-positive price), so symbol-independent, and 4,600 per-symbol calls cost
    # seconds that a single vectorised pass does not.
    try:
        from backtest.loaders.base import validate_ohlc

        frame = validate_ohlc(frame)
    except Exception as exc:  # noqa: BLE001 — validation is best-effort hygiene
        logger.debug("hub.bars_frame: validation skipped for %s: %s", token, exc)

    if frame.empty:
        return StoredBars(interval=token, frames={}, sources={}, derived_from=None)

    frames = bar_store.bars_by_symbol(frame)
    sources: dict[str, str] = {}
    if "source" in frame.columns and "date" in frame.columns:
        latest = (
            frame.dropna(subset=["source"])
            .sort_values(["symbol", "date"], kind="stable")
            .groupby("symbol", sort=False)["source"]
            .last()
        )
        sources = {str(symbol): str(value) for symbol, value in latest.items()}
    return StoredBars(
        interval=token, frames=frames, sources=sources, derived_from=derived_from
    )


def ensure_bars(
    symbols: Sequence[str],
    *,
    start: str,
    end: str,
    source: str = "auto",
    interval: str = "1D",
    network: Any,
    label: str = "data-center",
):
    """Serve daily bars from the lake, refreshing and storing a stale window.

    Features (monitor scans, market and sector picks, backtests, correlation)
    call this — or :func:`src.data.lake_fetch.ensure_bars`, which is the same
    function — instead of a loader. ``network`` is invoked only for the gap,
    and only when strict mode is off.
    """
    from src.data.lake_fetch import ensure_bars as _ensure_bars

    return _ensure_bars(
        list(symbols),
        start=start,
        end=end,
        source=source,
        interval=interval,
        network=network,
        label=label,
    )


def bars(
    interval: str = "1d",
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = BAR_POINT_LIMIT,
    adjust: str = "none",
) -> dict[str, Any]:
    """Read stored bars for the console's chart tab.

    Returns the envelope described in ``docs/design/data-center.md`` §5: the
    caller gets the data *and* the provenance needed to label it — which session
    this answers for, which sources contributed, how complete the window is and
    whether the payload was trimmed.

    The lake stores unadjusted prices plus ``adj_factor``; ``adjust`` is the one
    place that turns the factor into a comparable series (``"qfq"``/``"hfq"``),
    and it delegates to :func:`_rebase_on_factor` so a page never divides by a
    dividend it looked up itself.
    """
    from src.data import bars as bar_store

    as_of = resolve_as_of()
    requested = list(symbols) if symbols is not None else None
    mode = str(adjust or "none").lower()
    if mode not in {"none", "qfq", "hfq"}:
        raise ValueError(f"未知复权方式：{adjust!r}（可用 none / qfq / hfq）")
    capped: str | None = None
    if requested is None:
        # "No symbols" used to mean "every stored symbol".  With the full market
        # in the lake that is 5,000 series and a payload measured in gigabytes —
        # a chart cannot draw it and a browser cannot parse it.  Cap the *query*
        # (not the response) so the read stays cheap as well.
        from src.data import lake

        available = sorted(lake.symbol_spans(bar_store.DATASET_DAILY))
        if len(available) > MAX_SERIES:
            requested = available[:MAX_SERIES]
            capped = (
                f"未指定标的，湖内共 {len(available)} 个，只返回前 {MAX_SERIES} 个；"
                "请用 symbols 指定"
            )

    frame, derived_from = _read_bars_with_fallback(
        interval, symbols=requested, start=start, end=end
    )
    if frame.empty:
        return {
            "as_of": as_of.to_dict(),
            "interval": interval,
            "adjust": mode,
            "derived_from": None,
            "sources": [],
            "coverage": {"symbols": 0, "bars": 0, "sessions": 0, "first": None, "last": None},
            "series": {},
            "warnings": ["湖内没有该周期与区间的数据"],
        }

    dropped = 0
    if mode != "none":
        # Intraday rows have no factor column; the session's daily factor is the
        # honest one, so the two views of `adjust` stay one arithmetic.
        frame, dropped = _rebase_on_factor(_attach_daily_factor(frame), mode)

    sources = sorted({str(s) for s in frame.get("source", []) if str(s) not in {"", "nan", "None"}})
    if "source" not in frame.columns:
        warnings = ["该批数据落在 source 列之前，来源未知"]
    else:
        warnings = []
    if capped:
        warnings.append(capped)
    if dropped:
        warnings.append(f"{dropped} 根 K 线没有复权因子，已从复权视图中剔除")

    truncated: list[str] = []
    series: dict[str, dict[str, Any]] = {}
    for symbol, part in frame.groupby("symbol", sort=True):
        part = part.sort_values("date", kind="stable")
        if limit and len(part) > limit:
            truncated.append(str(symbol))
            part = part.tail(limit)
        series[str(symbol)] = {
            "bars": int(len(part)),
            "points": [
                {
                    "t": row.date.strftime("%Y-%m-%d %H:%M") if hasattr(row.date, "strftime") else str(row.date),
                    "o": _round(row.open),
                    "h": _round(row.high),
                    "l": _round(row.low),
                    "c": _round(row.close),
                    "v": _round(row.volume),
                }
                for row in part.itertuples(index=False)
            ],
        }

    if truncated:
        warnings.append(f"{len(truncated)} 个标的超过 {limit} 根，已截断到最近 {limit} 根")
    if derived_from:
        warnings.append(f"该周期未入库，已由 {derived_from} 本地按交易时段聚合")

    return {
        "as_of": as_of.to_dict(),
        "interval": interval,
        "adjust": mode,
        "derived_from": derived_from,
        "sources": sources,
        "coverage": {
            "symbols": int(frame["symbol"].nunique()),
            "bars": int(len(frame)),
            "sessions": int(pd.to_datetime(frame["date"]).dt.date.nunique()),
            "first": pd.to_datetime(frame["date"]).min().isoformat(),
            "last": pd.to_datetime(frame["date"]).max().isoformat(),
        },
        "series": series,
        "warnings": warnings,
    }


def adjusted_bars(
    interval: str = "1d",
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    mode: str = "qfq",
) -> pd.DataFrame:
    """Stored bars re-based on the lake's ``adj_factor`` — the only such place.

    Every consumer that wants a comparable price series (a multi-year backtest, a
    relative-strength scan, a "what did this return" answer) comes through here
    instead of dividing by a dividend it looked up itself.  The lake keeps prices
    unadjusted on purpose, so this conversion has exactly one definition:

    * ``"qfq"`` — 前复权: each symbol is scaled so its latest visible bar keeps the
      price the tape printed;
    * ``"hfq"`` — 后复权: multiply by the cumulative factor as stored.

    Volume and amount are never scaled: a dividend does not change a share count.
    ``pct_chg`` is recomputed from the adjusted closes, because the stored value is
    the *unadjusted* move and counts a payout as a loss on the ex-dividend day.

    Returns:
        Long frame sorted by ``symbol, date``.  Rows without a factor are dropped
        and counted in ``frame.attrs["missing_factor"]`` — adjusting them as if no
        dividend ever happened would be a silent lie.
    """
    if str(mode).lower() not in {"qfq", "hfq"}:
        raise ValueError(f"未知复权方式：{mode!r}（可用 qfq / hfq）")
    frame, _derived = _read_bars_with_fallback(
        interval, symbols=symbols, start=start, end=end
    )
    if frame.empty:
        return frame
    # Intraday bars carry no factor of their own — a dividend is a session-level
    # event, so every bar of a session inherits that session's factor.  Attaching
    # it here keeps exactly one scaling routine (`_rebase_on_factor`) instead of a
    # second intraday copy that could drift from the daily one.
    if "adj_factor" not in frame.columns:
        frame = _attach_daily_factor(frame)
    adjusted, dropped = _rebase_on_factor(frame, str(mode).lower())
    adjusted.attrs["adjust"] = str(mode).lower()
    adjusted.attrs["missing_factor"] = dropped
    return adjusted


def rebase_long_frame(frame: pd.DataFrame, mode: str = "qfq") -> pd.DataFrame:
    """Public view of :func:`_rebase_on_factor` for callers holding a long frame.

    The backtest loader already has ``adj_factor`` in hand, so it must not
    re-query the lake just to reuse the one rebasing routine. An empty result
    keeps the input columns so callers can fall back sanely.

    Args:
        frame: Long frame with ``symbol``, ``date`` and ``adj_factor``.
        mode: ``"qfq"`` or ``"hfq"``.

    Returns:
        The rebased frame; ``frame.attrs["missing_factor"]`` counts dropped rows.
    """
    adjusted, dropped = _rebase_on_factor(frame, str(mode).lower())
    adjusted.attrs["missing_factor"] = dropped
    adjusted.attrs["adjust"] = str(mode).lower()
    return adjusted


def _attach_daily_factor(frame: pd.DataFrame) -> pd.DataFrame:
    """Give each intraday row the ``adj_factor`` of its session.

    Backward ``merge_asof`` per symbol: a bar's session is normally present in
    the daily series, but the first bars of a resumption or a just-listed symbol
    can precede it — those take the most recent factor on or before the session,
    which is what the exchange means by "the factor in force that day".  Rows
    with no earlier factor are left null and dropped by the caller, counted.
    """
    if frame.empty:
        return frame

    from src.data import bars as bar_store

    if "adj_factor" in frame.columns and frame["adj_factor"].notna().any():
        return frame

    stamps = pd.to_datetime(frame["date"], errors="coerce")
    work = frame.copy()
    work["_session"] = stamps.dt.normalize()
    symbols = work["symbol"].astype(str).unique().tolist()

    # Look back a year before the first bar: the factor in force on a session can
    # come from months earlier (a suspension, a New Year holiday), and a query
    # that starts at the first session would miss every one of those rows.
    lookback = work["_session"].min() - pd.Timedelta(days=365)
    try:
        days = bar_store.load_bars(
            "1d",
            symbols=symbols,
            start=lookback.date().isoformat(),
            end=work["_session"].max().date().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - a missing factor is not a crash
        logger.debug("daily factor lookup failed: %s", exc)
        return frame

    if days.empty or "adj_factor" not in days.columns:
        return work.drop(columns=["_session"], errors="ignore")

    table = days[["symbol", "date", "adj_factor"]].copy()
    table["symbol"] = table["symbol"].astype(str)
    table["date"] = pd.to_datetime(table["date"], errors="coerce").dt.normalize()
    table = table.dropna(subset=["date", "adj_factor"])

    pieces = []
    for symbol, part in work.groupby("symbol", sort=False):
        mine = table[table["symbol"] == str(symbol)].sort_values("date")
        if mine.empty:
            pieces.append(part)
            continue
        right = mine[["adj_factor"]].copy()
        right["_factor_date"] = mine["date"].to_numpy()
        pieces.append(
            pd.merge_asof(
                part.sort_values("_session"),
                right,
                left_on="_session",
                right_on="_factor_date",
                direction="backward",
            )
        )

    out = pd.concat(pieces, ignore_index=True)
    return out.drop(columns=["_session", "_factor_date"], errors="ignore")


def _rebase_on_factor(frame: pd.DataFrame, mode: str) -> tuple[pd.DataFrame, int]:
    """Scale OHLC onto the stored factor; returns the frame and the dropped count.

    Private on purpose: :func:`bars` and :func:`adjusted_bars` are two views of
    one arithmetic, and a second copy of it is how "which adjustment was applied"
    stops having an answer.
    """
    if mode not in {"qfq", "hfq"}:
        raise ValueError(f"未知复权方式：{mode!r}（可用 qfq / hfq）")
    if "adj_factor" not in frame.columns:
        raise ValueError("该周期没有复权因子（只有日线落库 adj_factor），无法给出复权视图")

    work = frame.copy()
    work["adj_factor"] = pd.to_numeric(work["adj_factor"], errors="coerce")
    usable = work["adj_factor"].notna() & (work["adj_factor"] > 0)
    dropped = int((~usable).sum())
    work = work[usable].copy()
    if work.empty:
        return work, dropped

    work = work.sort_values(["symbol", "date"], kind="stable")
    if mode == "qfq":
        # Divide by the symbol's latest factor so the newest bar keeps its raw
        # price — the convention every chart and quote page uses.
        latest = work.groupby("symbol")["adj_factor"].transform("last")
        scale = work["adj_factor"] / latest
    else:
        scale = work["adj_factor"]
    for column in ("open", "high", "low", "close", "pre_close"):
        if column in work.columns:
            work[column] = pd.to_numeric(work[column], errors="coerce") * scale
    if "close" in work.columns:
        work["pct_chg"] = work.groupby("symbol")["close"].pct_change() * 100.0
    return work.reset_index(drop=True), dropped


def _round(value: Any, digits: int = 4) -> float | None:
    """Round a price/volume for transport; ``None`` when the cell is missing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def coverage_grid(
    interval: str = "1d",
    *,
    symbols: Sequence[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    max_symbols: int = 200,
    max_columns: int = 180,
) -> dict[str, Any]:
    """Presence matrix of symbol × session (or symbol × month), for the heat map.

    The gap view is the point of the console: a missing session shows up as a
    hole instead of being inferred from a chart's blank space.  Columns come
    from the *trading calendar*, not from the data, so a fully missing day still
    occupies a column — otherwise the heat map would happily draw a continuous
    line across a six-month outage.

    Long spans are bucketed by month (``granularity == "month"``) so a five-year
    view stays readable; ``covered``/``sessions`` then means "sessions with data
    in this month" out of the month's trading sessions.
    """
    from src.data import bars as bar_store
    from src.data import calendar

    as_of = resolve_as_of()
    empty = {
        "as_of": as_of.to_dict(),
        "interval": interval,
        "granularity": "session",
        "buckets": [],
        "symbols": [],
        "cells": [],
        "truncated": False,
        "warnings": ["湖内没有该周期与区间的数据"],
    }

    # Count inside the warehouse: the heat map is a "which sessions have data"
    # question, and answering it by loading 6M rows into pandas took 18 seconds
    # over the full market.  The bucket granularity has to be decided before the
    # query, so the window comes from the request or from one aggregate pass.
    first = start
    last = end
    if not (first and last):
        from src.data import lake
        from backtest.loaders._intervals import normalize_interval

        low, high, _ = lake.dataset_bounds(bar_store.dataset_for(normalize_interval(interval)))
        first = first or low
        last = last or high
    if not (first and last):
        return empty

    try:
        sessions = list(calendar.trading_days(first, last))
    except Exception:  # noqa: BLE001 - calendar is best effort
        sessions = []
    by_month = len(sessions) > max_columns

    counts = bar_store.session_counts(
        interval, symbols=symbols, start=start, end=end, by_month=by_month
    )
    derived_from: str | None = None
    if counts.empty:
        # A coarse interval (1H) may only exist as a finer one on disk, and that
        # aggregation is a resample, not a count — fall back to the frame path.
        frame, derived_from = _read_bars_with_fallback(
            interval, symbols=symbols, start=start, end=end
        )
        if frame.empty:
            return empty
        stamps = pd.to_datetime(frame["date"])
        raw = frame.assign(session=stamps.dt.strftime("%Y-%m-%d"))
        if not sessions:
            sessions = sorted(raw["session"].unique())
            by_month = len(sessions) > max_columns
        grouped = raw.groupby(["symbol", "session"]).size().reset_index(name="bars")
        grouped["bucket"] = (
            grouped["session"].str[:7] if by_month else grouped["session"]
        )
        counts = (
            grouped.groupby(["symbol", "bucket"]).size().reset_index(name="covered")
        )

    if not sessions:
        sessions = sorted(counts["bucket"].unique())

    if by_month:
        buckets = sorted({s[:7] for s in sessions})
        month_sessions = {
            bucket: sum(1 for s in sessions if s[:7] == bucket) for bucket in buckets
        }
    else:
        buckets = sessions
        month_sessions = {s: 1 for s in sessions}

    wanted = sorted(counts["symbol"].unique())
    truncated = len(wanted) > max_symbols
    wanted = wanted[:max_symbols]
    allowed = set(wanted)
    allowed_buckets = set(buckets)

    covered: dict[tuple[str, str], int] = {}
    for row in counts.itertuples(index=False):
        if row.symbol not in allowed or row.bucket not in allowed_buckets:
            continue
        covered[(row.symbol, row.bucket)] = int(row.covered)

    cells = [
        {
            "symbol": symbol,
            "bucket": bucket,
            "covered": covered.get((symbol, bucket), 0),
            "sessions": month_sessions.get(bucket, 0),
        }
        for symbol in wanted
        for bucket in buckets
    ]
    warnings = []
    if truncated:
        warnings.append(f"标的数超过 {max_symbols}，只显示前 {max_symbols} 个")
    if derived_from:
        warnings.append(f"覆盖度按 {derived_from} 原始数据统计")

    return {
        "as_of": as_of.to_dict(),
        "interval": interval,
        "granularity": "month" if by_month else "session",
        "buckets": [
            {"key": bucket, "sessions": month_sessions.get(bucket, 0)} for bucket in buckets
        ],
        "symbols": [
            {
                "symbol": symbol,
                "covered": sum(
                    covered.get((symbol, bucket), 0) for bucket in buckets
                ),
            }
            for symbol in wanted
        ],
        "cells": cells,
        "truncated": truncated,
        "warnings": warnings,
    }


def quota_status() -> dict[str, Any]:
    """Tushare allowance summary for the console header.

    Never raises: a missing token or a pool that cannot be built is itself the
    answer the console wants to show ("no tokens configured").
    """
    try:
        from backtest.loaders.tushare_pool import get_tushare_pool, resolve_tushare_tokens

        tokens = resolve_tushare_tokens()
        if not tokens:
            return {"tokens": 0, "endpoints": [], "note": "未配置 Tushare token"}
        report = get_tushare_pool().quota_report()
    except Exception as exc:  # noqa: BLE001
        return {"tokens": 0, "endpoints": [], "note": f"配额不可用：{exc}"}

    endpoints = [
        {
            "api": api,
            "per_token": int(payload.get("per_token") or 0),
            "tokens": int(payload.get("tokens") or 0),
            "remaining": int(payload.get("remaining") or 0),
        }
        for api, payload in sorted(report.items())
    ]
    return {"tokens": len(tokens), "endpoints": endpoints, "note": ""}


def overview(*, live: bool = False, refresh: bool = False) -> dict[str, Any]:
    """One call powering the console header and the overview tab."""
    from src.data import calendar

    as_of = resolve_as_of(live=live)
    if refresh:
        catalog.rebuild_all()

    cards = dataset_cards(as_of=as_of)
    with_data = [c for c in cards if (c["health"]["status"] != "empty")]
    empty = [c for c in cards if c["health"]["status"] == "empty"]

    try:
        calendar_source = calendar.calendar_source()
        has_calendar = calendar.has_calendar()
    except Exception:  # noqa: BLE001
        calendar_source, has_calendar = "unknown", False

    return {
        "as_of": as_of.to_dict(),
        "generated_at": now().isoformat(timespec="seconds"),
        "strict": strict_mode(),
        "lake": lake_stats(),
        "counts": {
            "declared": len(cards),
            "with_data": len(with_data),
            "empty": len(empty),
            "total_rows": sum(int(c["health"]["rows"] or 0) for c in cards),
        },
        "calendar": {"source": calendar_source, "available": has_calendar},
        "quota": quota_status(),
        "freshness": [c["health"] for c in cards],
        "alerts": quality_alerts(as_of=as_of),
    }


