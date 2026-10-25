"""Tests for adaptive loader host health / fallback reordering."""

from __future__ import annotations

import pytest

from backtest.loaders import source_health
from backtest.loaders.registry import (
    FALLBACK_CHAINS,
    LOADER_REGISTRY,
    fallback_chain_for,
    get_loader_cls_with_fallback,
    resolve_loader,
)
from backtest.loaders.source_health import (
    is_cooling_down,
    prefer_order,
    record_failure,
    record_success,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_health() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def test_prefer_order_boosts_recent_success() -> None:
    chain = ["tencent", "eastmoney", "akshare"]
    assert prefer_order(chain) == chain

    record_success("akshare")
    assert prefer_order(chain)[0] == "akshare"
    # Remaining hosts keep their relative order.
    assert prefer_order(chain)[1:] == ["tencent", "eastmoney"]


def test_circuit_opens_after_threshold_and_defers_source() -> None:
    for _ in range(source_health._FAIL_THRESHOLD):
        record_failure("tencent", TimeoutError("read timed out"))

    assert is_cooling_down("tencent")
    ordered = prefer_order(["tencent", "eastmoney", "akshare"])
    assert ordered[-1] == "tencent"
    assert ordered[0] == "eastmoney"


def test_success_clears_circuit() -> None:
    for _ in range(source_health._FAIL_THRESHOLD):
        record_failure("eastmoney", ConnectionError("aborted"))
    assert is_cooling_down("eastmoney")

    record_success("eastmoney")
    assert not is_cooling_down("eastmoney")
    assert prefer_order(["tencent", "eastmoney"])[0] == "eastmoney"


def test_fallback_chain_for_matches_static_when_healthy() -> None:
    assert fallback_chain_for("a_share") == FALLBACK_CHAINS["a_share"]


def test_resolve_loader_skips_cooling_host(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cool:
        name = "cool_src"
        markets = {"a_share"}

        def is_available(self) -> bool:
            return True

    class _Warm:
        name = "warm_src"
        markets = {"a_share"}

        def is_available(self) -> bool:
            return True

    monkeypatch.setitem(LOADER_REGISTRY, "cool_src", _Cool)
    monkeypatch.setitem(LOADER_REGISTRY, "warm_src", _Warm)
    monkeypatch.setitem(FALLBACK_CHAINS, "a_share", ["cool_src", "warm_src"])

    for _ in range(source_health._FAIL_THRESHOLD):
        record_failure("cool_src", TimeoutError("boom"))

    loader = resolve_loader("a_share")
    assert loader.name == "warm_src"


def test_get_loader_cls_falls_through_cooling_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cool:
        name = "cool_src"
        markets = {"a_share"}

        def is_available(self) -> bool:
            return True

    class _Warm:
        name = "warm_src"
        markets = {"a_share"}

        def is_available(self) -> bool:
            return True

    monkeypatch.setitem(LOADER_REGISTRY, "cool_src", _Cool)
    monkeypatch.setitem(LOADER_REGISTRY, "warm_src", _Warm)
    monkeypatch.setitem(FALLBACK_CHAINS, "a_share", ["cool_src", "warm_src"])

    for _ in range(source_health._FAIL_THRESHOLD):
        record_failure("cool_src", TimeoutError("boom"))

    cls = get_loader_cls_with_fallback("cool_src")
    assert cls is _Warm


def test_hard_transport_failure_opens_circuit_immediately() -> None:
    record_failure("tencent", Exception("HTTP Error 501: Not Implemented"))
    assert is_cooling_down("tencent") is True


def test_timeout_still_needs_threshold() -> None:
    record_failure("eastmoney", TimeoutError("timed out"))
    assert is_cooling_down("eastmoney") is False
    record_failure("eastmoney", TimeoutError("timed out"))
    assert is_cooling_down("eastmoney") is False
