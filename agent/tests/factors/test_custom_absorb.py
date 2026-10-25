"""Registry + numerical smoke tests for custom zoo alphas."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.factors.registry import Registry

# Legacy absorb + preferred quality/flow confirms.
_CUSTOM_IDS = [
    "custom_comp_low",
    "custom_near_low",
    "custom_ivol_inv",
    "custom_abn_to_inv",
    "custom_max_inv",
    "custom_vp_corr",
    "custom_risk_mom",
]

_QUALITY_IDS = [
    "custom_ivol_inv",
    "custom_abn_to_inv",
    "custom_max_inv",
    "custom_vp_corr",
    "custom_risk_mom",
]


def _seeded_panel(n_rows: int = 80, n_codes: int = 5) -> dict[str, pd.DataFrame]:
    """Reproducible OHLCV panel long enough for 60-bar warmup."""
    rng = np.random.RandomState(7)
    codes = [f"S{i}" for i in range(n_codes)]
    idx = pd.date_range("2024-01-01", periods=n_rows, freq="D")
    close = pd.DataFrame(
        np.abs(100.0 + np.cumsum(rng.normal(0, 1, (n_rows, n_codes)), axis=0)) + 1.0,
        index=idx,
        columns=codes,
    )
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.DataFrame(
        np.maximum(close.to_numpy(), open_.to_numpy())
        + rng.uniform(0, 1, (n_rows, n_codes)),
        index=idx,
        columns=codes,
    )
    low = (
        pd.DataFrame(
            np.minimum(close.to_numpy(), open_.to_numpy())
            - rng.uniform(0, 1, (n_rows, n_codes)),
            index=idx,
            columns=codes,
        ).abs()
        + 0.01
    )
    volume = pd.DataFrame(
        rng.randint(1000, 100000, (n_rows, n_codes)).astype(float),
        index=idx,
        columns=codes,
    )
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": volume * close,
    }


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry()


@pytest.mark.parametrize("alpha_id", _CUSTOM_IDS)
def test_custom_registered(registry: Registry, alpha_id: str) -> None:
    assert alpha_id in registry.list(zoo="custom")
    meta = registry.get(alpha_id).meta
    assert meta["universe"] == ["equity_cn"]


@pytest.mark.parametrize("alpha_id", _CUSTOM_IDS)
def test_custom_compute_shape(registry: Registry, alpha_id: str) -> None:
    panel = _seeded_panel()
    out = registry.compute(alpha_id, panel)
    assert out.shape == panel["close"].shape
    assert out.index.equals(panel["close"].index)
    assert list(out.columns) == list(panel["close"].columns)
    # After warmup the panel should have finite values on most cells.
    warmup = int(registry.get(alpha_id).meta["min_warmup_bars"])
    tail = out.iloc[warmup:]
    assert tail.notna().mean().mean() > 0.8
    assert np.isfinite(tail.to_numpy(dtype=np.float64)[tail.notna().to_numpy()]).all()


def test_comp_low_uses_nonneg_compression_gate(registry: Registry) -> None:
    """After warmup, finite custom_comp_low values should be >= 0 (compression clipped)."""
    panel = _seeded_panel()
    comp = registry.compute("custom_comp_low", panel)
    warmup = int(registry.get("custom_comp_low").meta["min_warmup_bars"])
    vals = comp.iloc[warmup:].to_numpy(dtype=np.float64)
    finite = vals[np.isfinite(vals)]
    assert finite.size > 0
    assert float(finite.min()) >= -1e-9


def test_ivol_inv_is_non_positive(registry: Registry) -> None:
    """IVOL_INV = -std(residual), so finite scores must be <= 0."""
    panel = _seeded_panel()
    out = registry.compute("custom_ivol_inv", panel)
    warmup = int(registry.get("custom_ivol_inv").meta["min_warmup_bars"])
    vals = out.iloc[warmup:].to_numpy(dtype=np.float64)
    finite = vals[np.isfinite(vals)]
    assert finite.size > 0
    assert float(finite.max()) <= 1e-9


def test_max_inv_penalizes_extreme_up_day(registry: Registry) -> None:
    """A single extreme up-day should lower custom_max_inv vs a calmer peer."""
    panel = _seeded_panel()
    close = panel["close"].copy()
    # Spike S0 on the last bar; leave S1 unchanged.
    close.iloc[-1, 0] = close.iloc[-2, 0] * 1.25
    panel["close"] = close

    out = registry.compute("custom_max_inv", panel)
    assert out.iloc[-1]["S0"] < out.iloc[-1]["S1"]


def test_custom_zoo_health(registry: Registry) -> None:
    health = registry.health()
    failed_ids = {e["alpha_id"] for e in health["errors"]}
    for alpha_id in _CUSTOM_IDS:
        assert alpha_id not in failed_ids
    assert set(registry.list(zoo="custom")) >= set(_CUSTOM_IDS)


def test_quality_factors_declared_as_confirm_friendly(registry: Registry) -> None:
    """Quality set should stay OHLCV-only and short-horizon for weekly rebalance."""
    for alpha_id in _QUALITY_IDS:
        meta = registry.get(alpha_id).meta
        assert meta["min_warmup_bars"] <= 60
        assert set(meta["columns_required"]).issubset(
            {"open", "high", "low", "close", "volume", "vwap", "amount"}
        )
