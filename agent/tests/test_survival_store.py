"""Tests for Alpha Zoo survival status persistence."""

from __future__ import annotations

import json

import pytest

from src.factors import survival_store as store


@pytest.fixture
def survival_tmp(tmp_path, monkeypatch):
    path = tmp_path / "alpha_survival.json"
    monkeypatch.setattr(store, "survival_path", lambda: path)
    return path


def test_record_and_read_roundtrip(survival_tmp):
    n = store.record_bench_survival(
        [
            {
                "id": "gtja191_001",
                "_category": "alive",
                "ic_mean": 0.03,
                "ir": 0.5,
            },
            {
                "id": "gtja191_002",
                "_category": "dead",
                "ic_mean": 0.0,
                "ir": 0.0,
            },
            {
                "id": "gtja191_003",
                "_category": "reversed",
                "ic_mean": -0.04,
                "ir": -0.8,
            },
        ],
        zoo="gtja191",
        universe="csi300",
        period="2020-2025",
    )
    assert n == 3
    assert survival_tmp.is_file()

    alive = store.get_status("gtja191_001")
    assert alive is not None
    assert alive["category"] == "alive"
    assert alive["alive_status"] == "alive"
    assert alive["universe"] == "csi300"

    dead = store.get_status("gtja191_002")
    assert dead is not None
    assert dead["alive_status"] == "invalid"

    reversed_row = store.get_status("gtja191_003")
    assert reversed_row is not None
    assert reversed_row["alive_status"] == "invalid"


def test_latest_bench_overwrites_previous_category(survival_tmp):
    store.record_bench_survival(
        [{"id": "alpha101_001", "_category": "alive", "ic_mean": 0.05, "ir": 1.0}],
        zoo="alpha101",
        universe="csi300",
        period="2020-2024",
    )
    store.record_bench_survival(
        [{"id": "alpha101_001", "_category": "dead", "ic_mean": 0.01, "ir": 0.1}],
        zoo="alpha101",
        universe="csi300",
        period="2020-2025",
    )
    rec = store.get_status("alpha101_001")
    assert rec is not None
    assert rec["category"] == "dead"
    assert rec["alive_status"] == "invalid"
    assert rec["period"] == "2020-2025"


def test_category_to_alive_status_mapping():
    assert store.category_to_alive_status("alive") == "alive"
    assert store.category_to_alive_status("dead") == "invalid"
    assert store.category_to_alive_status("reversed") == "invalid"
    assert store.category_to_alive_status(None) == "unknown"
    assert store.alive_status_of(None) == "unknown"


def test_atomic_write_produces_valid_json(survival_tmp):
    store.record_bench_survival(
        [{"id": "qlib158_001", "_category": "alive", "ic_mean": 0.04, "ir": 0.7}],
        zoo="qlib158",
        universe="csi300",
        period="2018-2022",
    )
    payload = json.loads(survival_tmp.read_text(encoding="utf-8"))
    assert "alphas" in payload
    assert "qlib158_001" in payload["alphas"]


def test_run_bench_persists_full_zoo_but_not_subset(monkeypatch, survival_tmp):
    """Full-zoo benches write survival; compare-style ``only=`` subsets do not."""
    import pandas as pd

    from src.factors import bench_runner as br

    close = pd.DataFrame(
        {"A": [10.0, 11.0, 12.0, 13.0], "B": [20.0, 19.0, 21.0, 22.0]},
        index=pd.date_range("2020-01-01", periods=4, freq="B"),
    )
    panel = {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": close * 100,
    }

    class _Alpha:
        def __init__(self, alpha_id: str):
            self.id = alpha_id
            self.zoo = "test_zoo"
            self.meta = {"theme": ["momentum"], "formula_latex": ""}

    class _Reg:
        def list(self, zoo=None, theme=None, universe=None):
            return ["test_zoo_a", "test_zoo_b"]

        def get(self, alpha_id):
            return _Alpha(alpha_id)

        def compute(self, alpha_id, panel_in):
            return panel_in["close"].pct_change(fill_method=None)

    calls: list[tuple[str, int]] = []

    def _capture(rows, *, zoo, universe, period):
        row_list = list(rows)
        calls.append((zoo, len(row_list)))
        return len(row_list)

    monkeypatch.setattr(br, "_load_universe_panel", lambda u, p: panel)
    monkeypatch.setattr(
        br,
        "_compute_forward_returns",
        lambda p: p["close"].pct_change(fill_method=None).shift(-1),
    )
    monkeypatch.setattr(
        "src.factors.survival_store.record_bench_survival",
        _capture,
    )

    full = br.run_bench("test_zoo", "mock", "2020-2021", registry=_Reg())
    assert full["status"] == "ok"
    assert calls == [("test_zoo", full["n_alphas_tested"])]

    calls.clear()
    subset = br.run_bench(
        "test_zoo",
        "mock",
        "2020-2021",
        registry=_Reg(),
        only=["test_zoo_a"],
    )
    assert subset["status"] == "ok"
    assert calls == []
