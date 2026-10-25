"""Refresh a bound run's report artifacts after a monitor scan.

Monitor scans write live boards/history under ``~/.vibe-trading/monitor/`` but
do not touch ``agent/runs/<run_id>/artifacts/``. RunDetail still reads those
artifacts for K-lines and buy/sell markers. This module bridges the gap:

1. Extend ``config.json`` / ``req.json`` ``end_date`` forward to the scan
   ``as_of`` (never shrink the original window).
2. Rebuild a full OHLCV panel by **merging** existing artifacts (and optional
   ``universe_run`` seed) with a gap fetch — never replace history with a
   short fallback window (e.g. Tencent's 500-bar cap).
3. Re-bench in-process so ``ohlcv_*.csv``, ``trades.csv``, ``equity.csv``, and
   metrics match the extended window.

Failures are fail-soft: the monitor board scan must still succeed even when
the report refresh cannot (missing engine, data outage, timeout).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from src.api.helpers import RUNS_DIR

logger = logging.getLogger(__name__)

# If artifact/history starts more than this many calendar days after config
# start_date, treat the panel as truncated and try to restore from seed/fetch.
_TRUNCATE_SLACK_DAYS = 45


def monitor_report_refresh_enabled() -> bool:
    """Whether scan should re-bench the bound run (default on)."""
    try:
        from src.config.accessor import get_env_config

        return bool(get_env_config().agent_tuning.vibe_trading_monitor_refresh_report)
    except Exception:  # noqa: BLE001 — default on if schema unavailable
        return True


def extend_run_end_date(run_dir: Path, as_of: date) -> dict[str, Any]:
    """Bump run ``end_date`` forward to ``as_of`` in config + request context.

    Returns a small status dict describing whether files changed. Raises
    ``ValueError`` when the run cannot be extended (missing config, bad dates).
    """
    config_path = run_dir / "config.json"
    if not config_path.is_file():
        raise ValueError(f"config.json not found under {run_dir}")

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"config.json parse error: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError("config.json must be a JSON object")

    start_raw = str(config.get("start_date") or "").strip()
    end_raw = str(config.get("end_date") or "").strip()
    as_of_iso = as_of.isoformat()

    if start_raw:
        try:
            start_date = date.fromisoformat(start_raw[:10])
        except ValueError as exc:
            raise ValueError(f"invalid config start_date: {start_raw!r}") from exc
        if as_of < start_date:
            raise ValueError(
                f"as_of {as_of_iso} is before start_date {start_raw[:10]}; refuse shrink"
            )

    previous_end = end_raw[:10] if end_raw else ""
    # Only move the window forward so historical report ranges stay intact.
    if previous_end:
        try:
            previous_end_date = date.fromisoformat(previous_end)
        except ValueError:
            previous_end_date = None
        if previous_end_date is not None and as_of < previous_end_date:
            return {
                "changed": False,
                "previous_end": previous_end,
                "end_date": previous_end,
                "reason": "as_of_before_existing_end",
            }

    if previous_end == as_of_iso:
        changed_config = False
    else:
        config["end_date"] = as_of_iso
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        changed_config = True

    changed_req = _sync_req_end_date(run_dir, as_of_iso)

    return {
        "changed": changed_config or changed_req,
        "previous_end": previous_end or None,
        "end_date": as_of_iso if (changed_config or not previous_end) else previous_end,
        "config_updated": changed_config,
        "req_updated": changed_req,
    }


def refresh_bound_run_report(
    run_id: str,
    as_of: date,
    *,
    runs_root: Path | None = None,
    run_backtest_fn: Any | None = None,
) -> dict[str, Any]:
    """Extend the bound run to ``as_of`` and regenerate report artifacts.

    Args:
        run_id: Strategy run directory name under ``runs/``.
        as_of: Inclusive end date for the refreshed backtest window.
        runs_root: Optional override for the runs root (tests).
        run_backtest_fn: Optional injectable ``run_backtest(run_dir) -> str``
            (defaults to history-preserving in-process re-bench).

    Returns:
        Status dict with ``ok`` / ``skipped`` / error detail. Never raises for
        expected operational failures.
    """
    rid = (run_id or "").strip()
    if not rid:
        return {"ok": False, "skipped": True, "reason": "empty_run_id"}

    if not monitor_report_refresh_enabled():
        return {"ok": False, "skipped": True, "reason": "disabled_by_env"}

    root = runs_root or RUNS_DIR
    run_dir = root / rid
    if not run_dir.is_dir():
        return {"ok": False, "skipped": True, "reason": "run_dir_missing", "run_id": rid}

    signal_path = run_dir / "code" / "signal_engine.py"
    if not signal_path.is_file():
        return {
            "ok": False,
            "skipped": True,
            "reason": "signal_engine_missing",
            "run_id": rid,
        }

    try:
        extend_info = extend_run_end_date(run_dir, as_of)
    except ValueError as exc:
        logger.warning("monitor report refresh: cannot extend %s: %s", rid, exc)
        return {"ok": False, "skipped": True, "reason": str(exc), "run_id": rid}

    # Drop stale hand-written price_series.csv so RunDetail prefers fresh
    # ohlcv_*.csv written by the engine after this re-bench.
    stale_price = run_dir / "artifacts" / "price_series.csv"
    if stale_price.is_file():
        try:
            stale_price.unlink()
        except OSError as exc:
            logger.warning("monitor report refresh: could not remove %s: %s", stale_price, exc)

    backtest = run_backtest_fn
    if backtest is None:
        backtest = lambda path: _run_bound_backtest(path, runs_root=root)

    refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        raw = backtest(str(run_dir))
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception as exc:  # noqa: BLE001 — fail-soft for the scan path
        logger.warning("monitor report refresh backtest failed for %s: %s", rid, exc)
        _write_refresh_stamp(
            run_dir,
            {
                "as_of": as_of.isoformat(),
                "refreshed_at": refreshed_at,
                "status": "error",
                "error": str(exc),
                "extend": extend_info,
            },
        )
        return {
            "ok": False,
            "run_id": rid,
            "as_of": as_of.isoformat(),
            "extend": extend_info,
            "error": str(exc),
        }

    status = str(payload.get("status") or "").lower()
    ok = status == "ok"
    stamp = {
        "as_of": as_of.isoformat(),
        "refreshed_at": refreshed_at,
        "status": "ok" if ok else "error",
        "extend": extend_info,
        "data": payload.get("data"),
        "backtest": {
            "status": payload.get("status"),
            "exit_code": payload.get("exit_code"),
            "error": payload.get("error"),
        },
    }
    if not ok:
        # Prefer stderr tail when the subprocess failed without a structured error.
        err_bits = [
            str(payload.get("error") or "").strip(),
            str(payload.get("stderr") or "").strip()[-500:],
        ]
        stamp["error"] = " | ".join(bit for bit in err_bits if bit) or "backtest failed"
        logger.warning(
            "monitor report refresh backtest status=%s for %s: %s",
            status,
            rid,
            stamp["error"],
        )
    else:
        logger.info(
            "monitor report refresh ok for %s through %s (end_date was %s)",
            rid,
            as_of.isoformat(),
            extend_info.get("previous_end"),
        )

    _write_refresh_stamp(run_dir, stamp)
    return {
        "ok": ok,
        "run_id": rid,
        "as_of": as_of.isoformat(),
        "extend": extend_info,
        "error": stamp.get("error"),
        "data": payload.get("data"),
    }


def _run_bound_backtest(run_dir: str, *, runs_root: Path | None = None) -> str:
    """Re-bench using merged historical OHLCV instead of a blind full re-fetch.

    Blind ``python -m backtest.runner`` re-fetches the whole window. Fallback
    sources such as Tencent hard-cap at ~500 bars, which silently drops early
    history (e.g. copper F0' 214% → ~82%). Prefer:

    1. existing ``artifacts/ohlcv_*.csv``
    2. optional ``req.json`` ``universe_run`` seed panel
    3. a gap (or repair) fetch merged on top
    """
    from backtest.loaders.registry import VALID_SOURCES
    from backtest.metrics import calc_bars_per_year
    from backtest.runner import (
        _AutoLoader,
        _create_market_engine,
        _load_module_from_file,
        fetch_data_map,
        validate_signal_engine_source,
    )
    from src.tools.path_utils import safe_run_dir

    try:
        from src.providers.llm import _ensure_dotenv

        _ensure_dotenv()
    except Exception:  # noqa: BLE001
        pass

    try:
        run_path = safe_run_dir(run_dir)
    except ValueError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    config_path = run_path / "config.json"
    if not config_path.is_file():
        return json.dumps({"status": "error", "error": "config.json not found"}, ensure_ascii=False)

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"status": "error", "error": f"config.json parse error: {exc}"},
            ensure_ascii=False,
        )

    source = config.get("source")
    if source not in VALID_SOURCES:
        return json.dumps(
            {
                "status": "error",
                "error": f"source must be one of {sorted(VALID_SOURCES)}, got: {source!r}",
            },
            ensure_ascii=False,
        )

    signal_path = run_path / "code" / "signal_engine.py"
    if not signal_path.is_file():
        return json.dumps(
            {"status": "error", "error": "code/signal_engine.py not found"},
            ensure_ascii=False,
        )

    try:
        validate_signal_engine_source(signal_path)
    except ValueError as exc:
        return json.dumps(
            {"status": "error", "error": f"SignalEngine source error: {exc}"},
            ensure_ascii=False,
        )

    start_date = str(config.get("start_date") or "").strip()[:10]
    end_date = str(config.get("end_date") or "").strip()[:10]
    codes = [str(c).strip() for c in (config.get("codes") or []) if str(c).strip()]
    if not codes or not start_date or not end_date:
        return json.dumps(
            {"status": "error", "error": "config missing codes/start_date/end_date"},
            ensure_ascii=False,
        )

    root = runs_root or RUNS_DIR
    existing = _load_ohlcv_dir(run_path / "artifacts")
    seed = _load_universe_seed(run_path, root)
    # Seed first so a previously truncated refresh can be repaired from the
    # research universe panel; then keep any newer bars already on disk.
    panel = _merge_ohlcv(_merge_ohlcv(seed, existing), {})

    coverage = _panel_coverage(panel, codes)
    needs_full_repair = _is_truncated(coverage.get("start"), start_date)
    fetch_start = start_date if needs_full_repair else _gap_fetch_start(coverage.get("end"), start_date)
    fetched: dict[str, pd.DataFrame] = {}
    fetch_sources: list[str] = []
    if fetch_start and fetch_start <= end_date:
        try:
            fetch_cfg = dict(config)
            fetch_cfg.update(codes=codes, start_date=fetch_start, end_date=end_date)
            fetch_result = fetch_data_map(fetch_cfg)
            fetched = dict(fetch_result.data_map or {})
            fetch_sources = list(fetch_result.effective_sources or [])
        except Exception as exc:  # noqa: BLE001 — keep seed/existing if gap fetch fails
            logger.warning("monitor report refresh gap fetch failed: %s", exc)

    panel = _merge_ohlcv(panel, fetched)
    # Clip to the configured visible window (seed may include lookback).
    panel = _clip_ohlcv(panel, start_date=start_date, end_date=end_date)
    panel = {c: panel[c] for c in codes if c in panel and not panel[c].empty}
    if len(panel) < max(1, min(3, len(codes))):
        return json.dumps(
            {
                "status": "error",
                "error": (
                    f"insufficient OHLCV after merge "
                    f"(have {len(panel)}/{len(codes)}; "
                    f"existing={len(existing)} seed={len(seed)} fetched={len(fetched)})"
                ),
            },
            ensure_ascii=False,
        )

    final_coverage = _panel_coverage(panel, list(panel.keys()))
    data_meta = {
        "existing_symbols": len(existing),
        "seed_symbols": len(seed),
        "fetched_symbols": len(fetched),
        "fetch_start": fetch_start,
        "fetch_sources": fetch_sources,
        "panel_start": final_coverage.get("start"),
        "panel_end": final_coverage.get("end"),
        "repaired_from_seed": bool(seed) and needs_full_repair,
    }
    if _is_truncated(final_coverage.get("start"), start_date):
        logger.warning(
            "monitor report refresh panel still truncated for %s: start=%s config_start=%s",
            run_path.name,
            final_coverage.get("start"),
            start_date,
        )

    try:
        signal_module = _load_module_from_file(
            signal_path,
            f"monitor_refresh_{run_path.name}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}",
        )
        engine_cls = getattr(signal_module, "SignalEngine", None)
        if engine_cls is None:
            return json.dumps(
                {"status": "error", "error": "SignalEngine class not found"},
                ensure_ascii=False,
            )
        signal_engine = engine_cls()

        run_cfg = dict(config)
        run_cfg["codes"] = list(panel.keys())
        run_cfg["_run_card_effective_sources"] = (
            ["local_artifacts"] + ([f"gap:{s}" for s in fetch_sources] if fetch_sources else [])
        )
        engine = _create_market_engine(str(source), run_cfg, run_cfg["codes"])
        interval = str(run_cfg.get("interval") or "1D")
        bars_per_year = calc_bars_per_year(interval, str(source))
        engine.run_backtest(
            run_cfg,
            _AutoLoader(panel),
            signal_engine,
            run_path,
            bars_per_year=bars_per_year,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("monitor report refresh in-process backtest failed")
        return json.dumps(
            {"status": "error", "error": str(exc), "data": data_meta},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "ok",
            "exit_code": 0,
            "artifacts": {
                "metrics": str(run_path / "artifacts" / "metrics.csv"),
                "equity": str(run_path / "artifacts" / "equity.csv"),
                "trades": str(run_path / "artifacts" / "trades.csv"),
            },
            "run_dir": str(run_path),
            "data": data_meta,
        },
        ensure_ascii=False,
    )


def _load_ohlcv_dir(artifacts: Path) -> dict[str, pd.DataFrame]:
    """Load ``ohlcv_*.csv`` frames keyed by symbol."""
    if not artifacts.is_dir():
        return {}
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(artifacts.glob("ohlcv_*.csv")):
        code = path.stem.removeprefix("ohlcv_")
        if not code:
            continue
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:  # noqa: BLE001
            continue
        if frame.empty:
            continue
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in frame.columns]
        if "close" not in keep:
            continue
        frame = frame[keep].sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        out[code] = frame
    return out


def _load_universe_seed(run_dir: Path, runs_root: Path) -> dict[str, pd.DataFrame]:
    """Load OHLCV from ``req.json`` / config ``universe_run`` when present."""
    universe_id = ""
    req_path = run_dir / "req.json"
    if req_path.is_file():
        try:
            req = json.loads(req_path.read_text(encoding="utf-8"))
            ctx = req.get("context") if isinstance(req, dict) else None
            if isinstance(ctx, dict):
                universe_id = str(ctx.get("universe_run") or "").strip()
        except (OSError, json.JSONDecodeError):
            universe_id = ""
    if not universe_id:
        try:
            cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
            params = cfg.get("parameters") if isinstance(cfg, dict) else None
            if isinstance(params, dict):
                universe_id = str(params.get("universe_run") or "").strip()
        except (OSError, json.JSONDecodeError):
            universe_id = ""
    if not universe_id:
        return {}
    seed_dir = runs_root / universe_id / "artifacts"
    if not seed_dir.is_dir():
        logger.info("monitor report refresh: universe_run %s not found", universe_id)
        return {}
    seed = _load_ohlcv_dir(seed_dir)
    if seed:
        logger.info(
            "monitor report refresh: loaded %d symbols from universe_run %s",
            len(seed),
            universe_id,
        )
    return seed


def _merge_ohlcv(
    base: dict[str, pd.DataFrame],
    incoming: dict[str, pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """Union panels; overlapping dates prefer ``incoming`` (newer fetch)."""
    out: dict[str, pd.DataFrame] = {k: v.copy() for k, v in base.items()}
    for code, frame in incoming.items():
        if frame is None or frame.empty:
            continue
        clean = frame.copy().sort_index()
        clean = clean[~clean.index.duplicated(keep="last")]
        if code not in out or out[code].empty:
            out[code] = clean
            continue
        # Keep older history from base; overwrite overlap with incoming.
        combined = pd.concat([out[code], clean], axis=0)
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        out[code] = combined
    return out


def _clip_ohlcv(
    panel: dict[str, pd.DataFrame],
    *,
    start_date: str,
    end_date: str,
) -> dict[str, pd.DataFrame]:
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    out: dict[str, pd.DataFrame] = {}
    for code, frame in panel.items():
        clipped = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if not clipped.empty:
            out[code] = clipped
    return out


def _panel_coverage(panel: dict[str, pd.DataFrame], codes: list[str]) -> dict[str, str | None]:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for code in codes:
        frame = panel.get(code)
        if frame is None or frame.empty:
            continue
        starts.append(pd.Timestamp(frame.index.min()))
        ends.append(pd.Timestamp(frame.index.max()))
    if not starts:
        return {"start": None, "end": None}
    # Use the latest common-ish start (median) so one late IPO does not dominate,
    # but for truncate detection we care about early history presence: use min.
    return {
        "start": min(starts).date().isoformat(),
        "end": max(ends).date().isoformat(),
    }


def _is_truncated(panel_start: str | None, config_start: str) -> bool:
    if not panel_start or not config_start:
        return True
    try:
        ps = date.fromisoformat(panel_start[:10])
        cs = date.fromisoformat(config_start[:10])
    except ValueError:
        return True
    return ps > cs + timedelta(days=_TRUNCATE_SLACK_DAYS)


def _gap_fetch_start(panel_end: str | None, config_start: str) -> str:
    """Fetch from a few days before the last known bar (overlap for adjustments)."""
    if not panel_end:
        return config_start
    try:
        end = date.fromisoformat(panel_end[:10])
    except ValueError:
        return config_start
    return (end - timedelta(days=7)).isoformat()


def _sync_req_end_date(run_dir: Path, as_of_iso: str) -> bool:
    """Keep RunDetail's ``load_run_context`` end_date in sync with config."""
    req_path = run_dir / "req.json"
    if not req_path.is_file():
        return False
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(req, dict):
        return False

    context = req.get("context")
    if not isinstance(context, dict):
        context = {}
        req["context"] = context

    previous = str(context.get("end_date") or "").strip()
    if previous[:10] == as_of_iso:
        return False

    # Same forward-only rule as config.json.
    if previous:
        try:
            if date.fromisoformat(previous[:10]) > date.fromisoformat(as_of_iso):
                return False
        except ValueError:
            pass

    context["end_date"] = as_of_iso
    try:
        req_path.write_text(
            json.dumps(req, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def _write_refresh_stamp(run_dir: Path, payload: dict[str, Any]) -> None:
    """Persist a small sidecar so operators can see the last live refresh."""
    artifacts = run_dir / "artifacts"
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "monitor_refresh.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("monitor refresh stamp write failed: %s", exc)
