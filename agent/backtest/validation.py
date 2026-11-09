"""Statistical validation for backtest results.

Three independent tools:
  - Monte Carlo permutation test: is the strategy significantly better than random?
  - Bootstrap Sharpe CI: how stable is the risk-adjusted return?
  - Walk-Forward analysis: is performance consistent across time windows?

Usage: called automatically by BaseEngine.run_backtest when config[\"validation\"]
is present, or invoked directly on backtest outputs.
"""

from __future__ import annotations

import json
import math
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Dict, List, Mapping

import numpy as np
import pandas as pd

from backtest.models import TradeRecord


# ─── Monte Carlo Permutation Test ───


def monte_carlo_test(
    trades: List[TradeRecord],
    initial_capital: float,
    n_simulations: int = 1000,
    seed: int = 42,
) -> Dict[str, Any]:
    """Shuffle trade PnL order to test path significance.

    Null hypothesis: the observed Sharpe / max-drawdown is no better than
    a random ordering of the same trades.

    Args:
        trades: Completed round-trip trades from backtest.
        initial_capital: Starting capital.
        n_simulations: Number of random permutations.
        seed: Random seed for reproducibility.

    Returns:
        Dict with actual_sharpe, p_value_sharpe, actual_max_dd,
        p_value_max_dd, simulated_sharpes (percentiles).
    """
    if isinstance(n_simulations, bool) or not isinstance(n_simulations, Integral) or n_simulations < 1:
        return {
            "error": f"n_simulations must be >= 1, got {n_simulations}",
            "p_value_sharpe": 1.0,
        }
    if isinstance(seed, bool) or not isinstance(seed, Integral) or seed < 0:
        return {"error": f"seed must be >= 0, got {seed}", "p_value_sharpe": 1.0}
    if len(trades) < 3:
        return {"error": "need at least 3 trades", "p_value_sharpe": 1.0}

    pnls = np.array([t.pnl for t in trades])
    actual = _path_metrics(pnls, initial_capital)

    rng = np.random.default_rng(seed)
    sharpe_count = 0
    dd_count = 0
    sim_sharpes = []

    for _ in range(n_simulations):
        shuffled = rng.permutation(pnls)
        sim = _path_metrics(shuffled, initial_capital)
        sim_sharpes.append(sim["sharpe"])
        if sim["sharpe"] >= actual["sharpe"]:
            sharpe_count += 1
        if sim["max_dd"] >= actual["max_dd"]:  # less negative = "better"
            dd_count += 1

    sim_arr = np.array(sim_sharpes)
    return {
        "actual_sharpe": round(actual["sharpe"], 4),
        "actual_max_dd": round(actual["max_dd"], 4),
        "p_value_sharpe": round(sharpe_count / n_simulations, 4),
        "p_value_max_dd": round(dd_count / n_simulations, 4),
        "simulated_sharpe_mean": round(float(sim_arr.mean()), 4),
        "simulated_sharpe_std": round(float(sim_arr.std()), 4),
        "simulated_sharpe_p5": round(float(np.percentile(sim_arr, 5)), 4),
        "simulated_sharpe_p95": round(float(np.percentile(sim_arr, 95)), 4),
        "n_simulations": n_simulations,
        "n_trades": len(trades),
    }


def _path_metrics(pnls: np.ndarray, initial_capital: float) -> Dict[str, float]:
    """Compute Sharpe and max drawdown from a PnL sequence."""
    equity = initial_capital + np.cumsum(pnls)
    returns = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([0.0])
    std = returns.std()
    sharpe = float(returns.mean() / (std + 1e-10) * np.sqrt(252))
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.where(peak > 0, peak, 1.0)
    max_dd = float(dd.min())
    return {"sharpe": sharpe, "max_dd": max_dd}


# ─── Bootstrap Sharpe CI ───


def bootstrap_sharpe_ci(
    equity_curve: pd.Series,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    bars_per_year: int = 252,
    seed: int = 42,
) -> Dict[str, Any]:
    """Resample daily returns to estimate Sharpe confidence interval.

    Args:
        equity_curve: Equity time series.
        n_bootstrap: Number of bootstrap samples.
        confidence: Confidence level (e.g. 0.95 for 95% CI).
        bars_per_year: Annualisation factor.
        seed: Random seed.

    Returns:
        Dict with observed_sharpe, ci_lower, ci_upper, median_sharpe,
        prob_positive (fraction of samples with Sharpe > 0).
    """
    if isinstance(n_bootstrap, bool) or not isinstance(n_bootstrap, Integral) or n_bootstrap < 1:
        return {"error": f"n_bootstrap must be >= 1, got {n_bootstrap}"}
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, Real)
        or not math.isfinite(float(confidence))
        or not 0.0 < confidence < 1.0
    ):
        return {"error": f"confidence must be in (0, 1), got {confidence}"}
    if isinstance(seed, bool) or not isinstance(seed, Integral) or seed < 0:
        return {"error": f"seed must be >= 0, got {seed}"}

    returns = equity_curve.pct_change().dropna().values
    if len(returns) < 5:
        return {"error": "need at least 5 return observations"}

    observed = _sharpe(returns, bars_per_year)

    rng = np.random.default_rng(seed)
    boot_sharpes = []
    for _ in range(n_bootstrap):
        sample = rng.choice(returns, size=len(returns), replace=True)
        boot_sharpes.append(_sharpe(sample, bars_per_year))

    arr = np.array(boot_sharpes)
    alpha = (1 - confidence) / 2
    lower = float(np.percentile(arr, alpha * 100))
    upper = float(np.percentile(arr, (1 - alpha) * 100))
    prob_pos = float(np.mean(arr > 0))

    return {
        "observed_sharpe": round(observed, 4),
        "ci_lower": round(lower, 4),
        "ci_upper": round(upper, 4),
        "median_sharpe": round(float(np.median(arr)), 4),
        "prob_positive": round(prob_pos, 4),
        "confidence": confidence,
        "n_bootstrap": n_bootstrap,
    }


def _sharpe(returns: np.ndarray, bars_per_year: int = 252) -> float:
    std = returns.std()
    return float(returns.mean() / (std + 1e-10) * np.sqrt(bars_per_year))


# ─── Walk-Forward Analysis ───


def walk_forward_analysis(
    equity_curve: pd.Series,
    trades: List[TradeRecord],
    n_windows: int = 5,
    bars_per_year: int = 252,
) -> Dict[str, Any]:
    """Split backtest into sequential windows, check consistency.

    Each window is evaluated independently (returns normalised to window start).

    Args:
        equity_curve: Equity time series.
        trades: Completed trades.
        n_windows: Number of non-overlapping windows.
        bars_per_year: Annualisation factor.

    Returns:
        Dict with per_window stats, consistency metrics.
    """
    if isinstance(n_windows, bool) or not isinstance(n_windows, Integral) or n_windows < 1:
        return {"error": f"n_windows must be >= 1, got {n_windows}"}
    if len(equity_curve) < n_windows * 2:
        return {"error": f"need at least {n_windows * 2} bars for {n_windows} windows"}

    indices = equity_curve.index
    window_size = len(indices) // n_windows
    windows = []

    for i in range(n_windows):
        start_idx = i * window_size
        end_idx = (i + 1) * window_size if i < n_windows - 1 else len(indices)
        win_eq = equity_curve.iloc[start_idx:end_idx]
        win_start = indices[start_idx]
        win_end = indices[end_idx - 1]

        # Per-window trades
        win_trades = [t for t in trades if win_start <= t.entry_time <= win_end]

        # Per-window metrics
        ret = float(win_eq.iloc[-1] / win_eq.iloc[0] - 1) if win_eq.iloc[0] > 0 else 0.0
        win_returns = win_eq.pct_change().dropna().values
        sharpe = _sharpe(win_returns, bars_per_year) if len(win_returns) > 1 else 0.0

        peak = win_eq.cummax()
        dd = (win_eq - peak) / peak.replace(0, 1)
        max_dd = float(dd.min())

        win_pnls = [t.pnl for t in win_trades]
        win_rate = len([p for p in win_pnls if p > 0]) / len(win_pnls) if win_pnls else 0.0

        windows.append(
            {
                "window": i + 1,
                "start": str(win_start.date()) if hasattr(win_start, "date") else str(win_start),
                "end": str(win_end.date()) if hasattr(win_end, "date") else str(win_end),
                "return": round(ret, 6),
                "sharpe": round(sharpe, 4),
                "max_dd": round(max_dd, 6),
                "trades": len(win_trades),
                "win_rate": round(win_rate, 4),
            }
        )

    # Consistency metrics
    returns_list = [w["return"] for w in windows]
    sharpes_list = [w["sharpe"] for w in windows]
    profitable_windows = sum(1 for r in returns_list if r > 0)

    return {
        "n_windows": n_windows,
        "windows": windows,
        "profitable_windows": profitable_windows,
        "consistency_rate": round(profitable_windows / n_windows, 4),
        "return_mean": round(float(np.mean(returns_list)), 6),
        "return_std": round(float(np.std(returns_list)), 6),
        "sharpe_mean": round(float(np.mean(sharpes_list)), 4),
        "sharpe_std": round(float(np.std(sharpes_list)), 4),
    }


def out_of_sample_split(
    equity_curve: pd.Series,
    oos_fraction: float = 0.3,
    bars_per_year: int = 252,
    min_bars: int = 20,
) -> Dict[str, Any]:
    """Compare in-sample vs. a held-out tail of the equity curve.

    The last ``oos_fraction`` of bars is the out-of-sample segment. A large
    Sharpe drop (``degradation``) points to overfitting.
    """
    if not isinstance(oos_fraction, Real) or isinstance(oos_fraction, bool) or not 0 < oos_fraction < 1:
        return {"error": f"oos_fraction must be in (0, 1), got {oos_fraction}"}
    n = len(equity_curve)
    split = n - int(round(n * float(oos_fraction)))
    if split < min_bars or n - split < min_bars:
        return {"error": f"need at least {min_bars} bars in each segment, got {n} total"}

    def _seg(eq: pd.Series) -> tuple[float, float]:
        rets = eq.pct_change().dropna().values
        ret = float(eq.iloc[-1] / eq.iloc[0] - 1) if eq.iloc[0] > 0 else 0.0
        return _sharpe(rets, bars_per_year), ret

    is_sharpe, is_ret = _seg(equity_curve.iloc[:split])
    # Start the OOS segment at the last in-sample bar so its first return counts.
    oos_sharpe, oos_ret = _seg(equity_curve.iloc[split - 1 :])
    if is_sharpe > 0:
        degradation = max(0.0, (is_sharpe - oos_sharpe) / is_sharpe)
    else:
        degradation = 0.0 if oos_sharpe >= is_sharpe else 1.0
    return {
        "oos_fraction": float(oos_fraction),
        "split": str(equity_curve.index[split]),
        "is_sharpe": round(is_sharpe, 4),
        "oos_sharpe": round(oos_sharpe, 4),
        "is_return": round(is_ret, 6),
        "oos_return": round(oos_ret, 6),
        "degradation": round(degradation, 4),
    }


# ─── Runner integration ───


def run_validation(
    config: Dict[str, Any],
    equity_curve: pd.Series,
    trades: List[TradeRecord],
    initial_capital: float,
    bars_per_year: int = 252,
) -> Dict[str, Any]:
    """Run configured validation checks.

    Reads from config["validation"]:
      - monte_carlo: {n_simulations, seed}
      - bootstrap: {n_bootstrap, confidence, seed}
      - walk_forward: {n_windows}
      - out_of_sample: {oos_fraction}

    Args:
        config: Backtest config (must contain "validation" key).
        equity_curve: Equity time series.
        trades: Completed trades.
        initial_capital: Starting capital.
        bars_per_year: Annualisation factor.

    Returns:
        Dict keyed by validation type with results.
    """
    v_cfg = config.get("validation", {})
    results: Dict[str, Any] = {}

    if "monte_carlo" in v_cfg:
        mc_cfg = v_cfg["monte_carlo"] if isinstance(v_cfg["monte_carlo"], dict) else {}
        results["monte_carlo"] = monte_carlo_test(
            trades,
            initial_capital,
            n_simulations=mc_cfg.get("n_simulations", 1000),
            seed=mc_cfg.get("seed", 42),
        )

    if "bootstrap" in v_cfg:
        bs_cfg = v_cfg["bootstrap"] if isinstance(v_cfg["bootstrap"], dict) else {}
        results["bootstrap"] = bootstrap_sharpe_ci(
            equity_curve,
            bars_per_year=bars_per_year,
            n_bootstrap=bs_cfg.get("n_bootstrap", 1000),
            confidence=bs_cfg.get("confidence", 0.95),
            seed=bs_cfg.get("seed", 42),
        )

    if "walk_forward" in v_cfg:
        wf_cfg = v_cfg["walk_forward"] if isinstance(v_cfg["walk_forward"], dict) else {}
        results["walk_forward"] = walk_forward_analysis(
            equity_curve,
            trades,
            n_windows=wf_cfg.get("n_windows", 5),
            bars_per_year=bars_per_year,
        )

    if "out_of_sample" in v_cfg:
        oos_cfg = v_cfg["out_of_sample"] if isinstance(v_cfg["out_of_sample"], dict) else {}
        results["out_of_sample"] = out_of_sample_split(
            equity_curve,
            oos_fraction=oos_cfg.get("oos_fraction", 0.3),
            bars_per_year=bars_per_year,
        )

    if results:
        # Stamp the cost schedule that produced these trades so the monitor
        # gate can refuse a zero-cost artifact. Empty ``validation`` config
        # still returns {} — callers that skip validation stay unchanged.
        results["cost_aware"] = config_is_cost_aware(config)
        results["costs"] = cost_snapshot(config)
        # Stamp the annualisation factor too: a later recompute from the CSV
        # artifacts cannot infer it (a mixed-market run passes None), and using
        # a different factor would move every Sharpe without the data changing.
        results["bars_per_year"] = bars_per_year

    return results


# ─── Standalone CLI ───


def _load_equity(run_dir: Path) -> pd.Series:
    """Load equity curve from artifacts/equity.csv."""
    path = run_dir / "artifacts" / "equity.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    for col in ("equity", "nav", "value"):
        if col in df.columns:
            return df[col]
    raise ValueError(
        f"equity.csv must contain an equity/nav/value column; got {list(df.columns)}"
    )


def _load_trades(run_dir: Path) -> List[TradeRecord]:
    """Load trades from artifacts/trades.csv and convert to TradeRecord list.

    ``trades.csv`` holds one row per fill (a buy row and a sell row per round
    trip), while validation reasons about *closed* trades. Each sell is paired
    with the oldest unclosed buy of the same code so the record carries the
    real entry time: walk-forward attributes a trade to a window by
    ``entry_time``, and using the exit timestamp instead moves trades across
    window boundaries — the recompute then disagrees with the engine on trade
    counts even though both read the same file.
    """
    path = run_dir / "artifacts" / "trades.csv"
    df = pd.read_csv(path)
    if df.empty:
        return []

    has_side = "side" in df.columns
    open_lots: Dict[str, List[tuple[pd.Timestamp, float]]] = {}
    trades = []

    for _, row in df.iterrows():
        code = str(row.get("code", ""))
        timestamp = pd.Timestamp(row.get("timestamp", "2000-01-01"))
        side = str(row.get("side", "")).strip().lower()
        price = _row_float(row.get("price", 0))
        pnl = _row_float(row.get("pnl", 0))

        if has_side and side == "buy":
            open_lots.setdefault(code, []).append((timestamp, price))
            continue
        # An exit row is a sell. A break-even exit (pnl == 0) is still a closed
        # trade and the engine counts it, so it must not be filtered out here.
        if not has_side and pnl == 0:
            continue

        entry_time = timestamp
        entry_price = 0.0
        lots = open_lots.get(code)
        if lots:
            entry_time, entry_price = lots.pop(0)

        hold = pd.to_numeric(row.get("holding_days", 0), errors="coerce")
        holding_bars = 0 if pd.isna(hold) else int(hold)
        trades.append(
            TradeRecord(
                symbol=code,
                direction=1 if side == "sell" else -1,
                entry_price=entry_price,
                exit_price=price,
                entry_time=entry_time,
                exit_time=timestamp,
                size=float(row.get("qty", 0)),
                leverage=1.0,
                pnl=pnl,
                pnl_pct=_row_float(row.get("return_pct", 0)),
                exit_reason=str(row.get("reason", "signal")),
                holding_bars=holding_bars,
                commission=0.0,
            )
        )
    return trades


def _row_float(value: Any) -> float:
    """Coerce a CSV cell to float, treating blanks/None as 0.0."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_run_dir(argv: List[str]) -> Path:
    """Validate CLI input and return a usable run directory path."""
    if len(argv) < 2:
        raise SystemExit("Usage: python -m backtest.validation <run_dir>")

    raw_run_dir = argv[1]
    if not raw_run_dir.strip():
        raise SystemExit("run_dir must be a non-empty path")
    if "\0" in raw_run_dir:
        raise SystemExit("Invalid run_dir path: embedded NUL byte")

    try:
        run_dir = Path(raw_run_dir).expanduser()
        exists = run_dir.exists()
        is_dir = run_dir.is_dir() if exists else False
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Invalid run_dir path: {exc}") from exc

    if not exists:
        raise SystemExit(f"run_dir does not exist: {run_dir}")
    if not is_dir:
        raise SystemExit(f"run_dir is not a directory: {run_dir}")
    return run_dir


def _json_safe(value: Any) -> Any:
    """Return a JSON-strict copy of validation results."""
    if isinstance(value, np.ndarray):
        return [_json_safe(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_validation_json(path: Path, results: Dict[str, Any]) -> Dict[str, Any]:
    """Write validation results to ``path`` as strict, RFC-8259 JSON.

    A validation metric can be non-finite (e.g. a Sharpe computed from a path
    whose equity touches zero), and ``json.dumps`` emits bare ``NaN`` /
    ``Infinity`` tokens for those by default (``allow_nan=True``) — tokens that
    strict parsers reject. Sanitize with :func:`_json_safe` (non-finite → null)
    and serialize with ``allow_nan=False`` so every writer of
    ``artifacts/validation.json`` produces the same valid JSON. Returns the
    sanitized payload that was written.
    """
    safe_results = _json_safe(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(safe_results, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return safe_results


# ─── Cost-aware promotion gate ───
#
# A strategy may enter the monitor tradable pool only when Monte Carlo,
# bootstrap, and walk-forward each clear a threshold AND the backtest that
# produced the artifact paid a non-zero cost schedule. A-share books pick up
# the P0 retail defaults (``backtest.ashare_costs``) when the run config omits
# the keys. Thresholds are read from ``DataConfig``; see the
# ``VIBE_TRADING_VAL_*`` variables.

_FEE_KEYS = (
    "commission",
    "commission_rate",
    "commission_min",
    "stamp_tax",
    "stamp_duty",
    "transfer_fee",
    "slippage",
)

_ASHARE_MARKETS = {"china_a", "a_share", "ashare", "cn"}


def _as_positive(value: Any) -> bool:
    """True when *value* is a finite number greater than zero."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def _positive_fee(payload: Mapping[str, Any] | None) -> bool:
    """True when any recognised fee field is a positive number."""
    if not isinstance(payload, Mapping):
        return False
    return any(_as_positive(payload.get(key)) for key in _FEE_KEYS)


def _config_codes(config: Mapping[str, Any]) -> list[str]:
    raw = config.get("codes") or config.get("symbols") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        return []
    return [str(code).strip() for code in raw if str(code).strip()]


def book_uses_ashare_defaults(config: Mapping[str, Any] | None) -> bool:
    """True when this run is filled by ``ChinaAEngine`` (P0 cost schedule).

    Mirrors runner routing closely enough for the gate: an explicit A-share
    market, a tushare/akshare book that is not US/HK, or a code list that is
    entirely explicit A-share listings (lake / local / eastmoney).
    """
    cfg = dict(config or {})
    market = str(cfg.get("market") or "").strip().lower()
    engine = str(cfg.get("engine") or "").strip().lower()
    if market in _ASHARE_MARKETS or engine in _ASHARE_MARKETS:
        return True
    codes = _config_codes(cfg)
    try:
        from backtest.engines._market_hooks import _is_explicit_a_share
    except Exception:  # pragma: no cover — hooks are part of the backtest package
        _is_explicit_a_share = None  # type: ignore[assignment]
    if codes and _is_explicit_a_share is not None and all(_is_explicit_a_share(code) for code in codes):
        return True
    source = str(cfg.get("source") or "").strip().lower()
    if source in {"tushare", "akshare"}:
        if codes and any(code.upper().endswith((".US", ".HK")) for code in codes):
            return False
        return True
    return False


def cost_snapshot(config: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Fee keys explicitly present on *config* (defaults are not copied)."""
    cfg = dict(config or {})
    return {key: cfg[key] for key in _FEE_KEYS if key in cfg}


def config_is_cost_aware(config: Mapping[str, Any] | None) -> bool:
    """True when the backtest paid a non-zero commission, tax, or slippage.

    An A-share book with the keys omitted is cost-aware: ``ChinaAEngine``
    merges the P0 retail defaults. An explicit zero on every fee key opts out.
    """
    cfg = dict(config or {})
    if _positive_fee(cfg) or cfg.get("cost_aware") is True:
        return True
    if not book_uses_ashare_defaults(cfg):
        return False
    try:
        from backtest.ashare_costs import merge_assumptions

        merged = merge_assumptions(cfg)
    except Exception:  # noqa: BLE001 — a missing helper must not look cost-free by accident
        merged = cfg
    return _positive_fee(merged)


def costs_applied(
    results: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    """True when the validation artifact or the run config shows real costs."""
    if isinstance(results, Mapping):
        if results.get("cost_aware") is True:
            return True
        costs = results.get("costs")
        if _positive_fee(costs if isinstance(costs, Mapping) else None):
            return True
    return config_is_cost_aware(config)


class ValidationThresholds:
    """Promotion thresholds for Monte Carlo, bootstrap, and walk-forward.

    Defaults (override with ``VIBE_TRADING_VAL_*``):

    * Monte Carlo ``p_value_sharpe`` ≤ 0.10
    * Bootstrap Sharpe ``ci_lower`` ≥ 0
    * Walk-forward ``consistency_rate`` ≥ 0.60
    * Costs required
    * All three sections required
    * Gate enabled
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_p_value_sharpe: float = 0.10,
        min_bootstrap_ci_lower: float = 0.0,
        min_walk_forward_consistency: float = 0.60,
        require_costs: bool = True,
        require_monte_carlo: bool = True,
        require_bootstrap: bool = True,
        require_walk_forward: bool = True,
        require_out_of_sample: bool = False,
        max_oos_degradation: float = 0.5,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_p_value_sharpe = float(max_p_value_sharpe)
        self.min_bootstrap_ci_lower = float(min_bootstrap_ci_lower)
        self.min_walk_forward_consistency = float(min_walk_forward_consistency)
        self.require_costs = bool(require_costs)
        self.require_monte_carlo = bool(require_monte_carlo)
        self.require_bootstrap = bool(require_bootstrap)
        self.require_walk_forward = bool(require_walk_forward)
        self.require_out_of_sample = bool(require_out_of_sample)
        self.max_oos_degradation = float(max_oos_degradation)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_p_value_sharpe": self.max_p_value_sharpe,
            "min_bootstrap_ci_lower": self.min_bootstrap_ci_lower,
            "min_walk_forward_consistency": self.min_walk_forward_consistency,
            "require_costs": self.require_costs,
            "require_monte_carlo": self.require_monte_carlo,
            "require_bootstrap": self.require_bootstrap,
            "require_walk_forward": self.require_walk_forward,
            "require_out_of_sample": self.require_out_of_sample,
            "max_oos_degradation": self.max_oos_degradation,
        }


def thresholds_from_env() -> ValidationThresholds:
    """Read ``VIBE_TRADING_VAL_*`` (via ``DataConfig``) into thresholds."""
    try:
        from src.config.accessor import get_env_config

        data = get_env_config().data
    except Exception:  # noqa: BLE001 — documented defaults still gate promotion
        return ValidationThresholds()
    return ValidationThresholds(
        enabled=bool(getattr(data, "vibe_trading_val_gate", True)),
        max_p_value_sharpe=float(getattr(data, "vibe_trading_val_max_p_sharpe", 0.10)),
        min_bootstrap_ci_lower=float(getattr(data, "vibe_trading_val_min_bootstrap_ci", 0.0)),
        min_walk_forward_consistency=float(
            getattr(data, "vibe_trading_val_min_wf_consistency", 0.60)
        ),
        require_costs=bool(getattr(data, "vibe_trading_val_require_costs", True)),
        require_monte_carlo=bool(getattr(data, "vibe_trading_val_require_monte_carlo", True)),
        require_bootstrap=bool(getattr(data, "vibe_trading_val_require_bootstrap", True)),
        require_walk_forward=bool(
            getattr(data, "vibe_trading_val_require_walk_forward", True)
        ),
        require_out_of_sample=bool(
            getattr(data, "vibe_trading_val_require_out_of_sample", False)
        ),
        max_oos_degradation=float(
            getattr(data, "vibe_trading_val_max_oos_degradation", 0.5)
        ),
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


class ValidationGateResult:
    """Outcome of :func:`evaluate_validation_gate`."""

    def __init__(
        self,
        *,
        passed: bool,
        reasons: List[str],
        cost_aware: bool,
        checks: Dict[str, Any],
        thresholds: Dict[str, Any],
    ) -> None:
        self.passed = passed
        self.reasons = list(reasons)
        self.cost_aware = cost_aware
        self.checks = checks
        self.thresholds = thresholds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "cost_aware": self.cost_aware,
            "checks": self.checks,
            "thresholds": self.thresholds,
        }


def evaluate_validation_gate(
    results: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
    thresholds: ValidationThresholds | None = None,
    load_error: str | None = None,
) -> ValidationGateResult:
    """Decide whether a validation artifact may enter the tradable pool.

    ``load_error`` is ``validation_unreadable`` or ``config_unreadable`` when
    the run directory could not be parsed. A missing artifact (``results is
    None`` and no load error) is ``missing_validation``.

    When the gate is disabled (``VIBE_TRADING_VAL_GATE=0``) the check passes
    and ``checks["enabled"]`` is false. The decision journal is a separate
    requirement and is not waived here.
    """
    rules = thresholds if thresholds is not None else thresholds_from_env()
    aware = costs_applied(results if isinstance(results, Mapping) else None, config)
    if not rules.enabled:
        return ValidationGateResult(
            passed=True,
            reasons=[],
            cost_aware=aware,
            checks={"enabled": False, "note": "gate_disabled"},
            thresholds=rules.to_dict(),
        )

    if load_error:
        return ValidationGateResult(
            passed=False,
            reasons=[load_error],
            cost_aware=aware,
            checks={"enabled": True},
            thresholds=rules.to_dict(),
        )

    if not isinstance(results, Mapping) or not any(
        key in results for key in ("monte_carlo", "bootstrap", "walk_forward")
    ):
        # No artifact means no evidence either way, including on costs. Stamping
        # ``costs.passed = false`` here made a missing file look like a
        # zero-cost backtest, and readers reported "costs not applied" for a run
        # whose artifact had simply been deleted.
        return ValidationGateResult(
            passed=False,
            reasons=["missing_validation"],
            cost_aware=aware,
            checks={"enabled": True},
            thresholds=rules.to_dict(),
        )

    reasons: List[str] = []
    checks: Dict[str, Any] = {"enabled": True}

    if rules.require_costs:
        checks["costs"] = {"passed": aware}
        if not aware:
            reasons.append("costs_not_applied")
    else:
        checks["costs"] = {"passed": True, "skipped": True}

    section_specs = (
        (
            "monte_carlo",
            rules.require_monte_carlo,
            "missing_monte_carlo",
            "monte_carlo_failed",
            "p_value_sharpe",
            "monte_carlo_p_value",
            rules.max_p_value_sharpe,
            "max",
        ),
        (
            "bootstrap",
            rules.require_bootstrap,
            "missing_bootstrap",
            "bootstrap_failed",
            "ci_lower",
            "bootstrap_ci_lower",
            rules.min_bootstrap_ci_lower,
            "min",
        ),
        (
            "walk_forward",
            rules.require_walk_forward,
            "missing_walk_forward",
            "walk_forward_failed",
            "consistency_rate",
            "walk_forward_consistency",
            rules.min_walk_forward_consistency,
            "min",
        ),
    )
    for (
        name,
        required,
        missing_code,
        failed_code,
        metric,
        breach_code,
        limit,
        direction,
    ) in section_specs:
        section = results.get(name)
        if not required:
            checks[name] = {"passed": True, "skipped": True}
            continue
        if not isinstance(section, Mapping):
            reasons.append(missing_code)
            checks[name] = {"passed": False, "reason": missing_code}
            continue
        if section.get("error"):
            reasons.append(failed_code)
            checks[name] = {
                "passed": False,
                "reason": failed_code,
                "detail": str(section.get("error")),
            }
            continue
        observed = _finite(section.get(metric))
        if observed is None:
            reasons.append(failed_code)
            checks[name] = {"passed": False, "reason": failed_code, "metric": metric}
            continue
        ok = observed <= limit if direction == "max" else observed >= limit
        if not ok:
            reasons.append(breach_code)
        checks[name] = {
            "passed": ok,
            "metric": metric,
            "observed": observed,
            "threshold": limit,
            **({"reason": breach_code} if not ok else {}),
        }

    if rules.require_out_of_sample:
        oos = results.get("out_of_sample")
        if not isinstance(oos, Mapping):
            reasons.append("missing_out_of_sample")
            checks["out_of_sample"] = {"passed": False, "reason": "missing_out_of_sample"}
        elif oos.get("error"):
            reasons.append("out_of_sample_failed")
            checks["out_of_sample"] = {
                "passed": False,
                "reason": "out_of_sample_failed",
                "detail": str(oos.get("error")),
            }
        else:
            deg = _finite(oos.get("degradation"))
            oos_sharpe = _finite(oos.get("oos_sharpe"))
            if deg is None or oos_sharpe is None:
                reasons.append("out_of_sample_failed")
                checks["out_of_sample"] = {"passed": False, "reason": "out_of_sample_failed"}
            else:
                ok = deg <= rules.max_oos_degradation and oos_sharpe > 0
                if not ok:
                    reasons.append("out_of_sample_degraded")
                checks["out_of_sample"] = {
                    "passed": ok,
                    "observed": deg,
                    "oos_sharpe": oos_sharpe,
                    "threshold": rules.max_oos_degradation,
                    **({"reason": "out_of_sample_degraded"} if not ok else {}),
                }

    return ValidationGateResult(
        passed=not reasons,
        reasons=reasons,
        cost_aware=aware,
        checks=checks,
        thresholds=rules.to_dict(),
    )


def load_run_validation(run_dir: Path) -> tuple[Dict[str, Any] | None, Dict[str, Any], str | None]:
    """Load ``config.json`` and ``artifacts/validation.json`` for the gate.

    Returns:
        ``(results, config, load_error)``. ``results`` is None when the
        artifact is missing. ``load_error`` is set when a file exists but
        cannot be parsed.
    """
    directory = Path(run_dir)
    config: Dict[str, Any] = {}
    config_path = directory / "config.json"
    if config_path.is_file():
        try:
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, {}, "config_unreadable"
        if not isinstance(raw_config, dict):
            return None, {}, "config_unreadable"
        config = raw_config

    validation_path = directory / "artifacts" / "validation.json"
    if not validation_path.is_file():
        return None, config, None
    try:
        raw_results = json.loads(validation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, config, "validation_unreadable"
    if not isinstance(raw_results, dict):
        return None, config, "validation_unreadable"
    return raw_results, config, None


def evaluate_run_validation(
    run_dir: Path,
    *,
    thresholds: ValidationThresholds | None = None,
) -> ValidationGateResult:
    """Load a run directory and evaluate the promotion gate."""
    results, config, load_error = load_run_validation(run_dir)
    return evaluate_validation_gate(
        results,
        config=config,
        thresholds=thresholds,
        load_error=load_error,
    )


def main(run_dir: Path) -> Dict[str, Any]:
    """Recompute the three validations from an existing run's artifacts.

    Reads equity.csv, trades.csv, and config.json from run_dir.

    This is the reconciliation path (§8.3): it must reproduce the numbers the
    engine wrote, not merely produce plausible ones. Two things are therefore
    carried over from the run instead of taken from function defaults:

    * the ``validation`` knobs (simulation counts, seeds, window count) — a run
      that used ``n_simulations=300`` must be rechecked at 300, otherwise every
      p-value moves for a reason that has nothing to do with the data;
    * ``bars_per_year`` — recorded in the previous validation.json, because the
      annualisation factor comes from the run's source/interval and a mixed
      market run deliberately uses calendar-day annualisation.

    Args:
        run_dir: Directory with artifacts/ subdirectory.

    Returns:
        Validation results dict.
    """
    # Load config for initial_cash, cost schedule, and validation knobs.
    config_path = run_dir / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {}
    initial_capital = config.get("initial_cash", 1_000_000)

    equity = _load_equity(run_dir)
    trades = _load_trades(run_dir)


    results = {
        "monte_carlo": monte_carlo_test(trades, initial_capital),
        "bootstrap": bootstrap_sharpe_ci(equity),
        "walk_forward": walk_forward_analysis(equity, trades),
        "out_of_sample": out_of_sample_split(equity),
        "cost_aware": config_is_cost_aware(config),
        "costs": cost_snapshot(config),
    }

    out = run_dir / "artifacts" / "validation.json"
    bars_per_year = _carried_bars_per_year(out, config)
    effective = _effective_validation_config(config)



    safe_results = write_validation_json(out, results)

    print(json.dumps(safe_results, indent=2, allow_nan=False))
    return safe_results


def _effective_validation_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Return *config* with the three checks always requested.

    ``run_validation`` returns ``{}`` for a run that never asked for validation.
    The standalone command is only ever pointed at a finished run, and its
    contract is "recompute all three", so an absent block means "defaults" —
    not "nothing to do".
    """
    effective = dict(config)
    declared = effective.get("validation")
    if not isinstance(declared, Mapping) or not declared:
        effective["validation"] = {"monte_carlo": {}, "bootstrap": {}, "walk_forward": {}}
    return effective


def _carried_bars_per_year(path: Path, config: Mapping[str, Any]) -> int | None:
    """Annualisation factor to reuse when rechecking a run.

    Prefers the value the engine stamped into its own validation.json (exact
    by construction). Falls back to the config's interval/source, and finally
    to 252 daily bars. ``None`` means calendar-day annualisation, which is what
    a cross-market run uses.
    """
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if isinstance(previous, Mapping):
        carried = previous.get("bars_per_year")
        if isinstance(carried, int) and not isinstance(carried, bool) and carried > 0:
            return carried

    try:
        from backtest.metrics import calc_bars_per_year
    except Exception:  # pragma: no cover - metrics is a hard dependency
        return 252
    interval = str(config.get("interval") or "1D")
    source = str(config.get("source") or "tushare")
    try:
        return calc_bars_per_year(interval, source)
    except Exception:
        return 252


if __name__ == "__main__":
    import sys

    main(_parse_run_dir(sys.argv))
