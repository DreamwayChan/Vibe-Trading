"""Fail-soft branches of the research-only wind scan."""

from __future__ import annotations

from datetime import date

import pandas as pd

from src.sector_pick.catalog import CATALOG
from src.sector_pick.wind_scan import evaluate_sector_wind, run_wind_cards, run_wind_scan


def stamp_card(sector: str) -> dict:
    return {"sector": sector, "cards": [{"factor": "kept"}], "tradable": False}


def _bars(n: int = 40, m: int = 4):
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.DataFrame({i: 100.0 + i for i in range(m)}, index=idx)
    frames = {}
    for i in range(m):
        frame = pd.DataFrame(
            {
                "open": close[i],
                "close": close[i],
                "volume": 1000.0,
                "amount": 1000.0 * close[i],
            },
            index=idx,
        )
        frames[f"{600000 + i}.SH"] = frame
    bench = pd.DataFrame(
        {"open": 100.0, "close": 100.0, "volume": 1000.0, "amount": 100_000.0},
        index=idx,
    )
    return frames, bench


def test_too_few_members_is_still_not_tradable():
    frames, bench = _bars(m=2)
    out = evaluate_sector_wind(frames, bench)
    assert out["status"] == "insufficient_members"
    assert out["tradable"] is False
    assert out["block_reason"] == "uncausal_scan"


def test_run_wind_scan_seals_every_failure(monkeypatch, tmp_path):
    import src.sector_pick.wind_scan as ws

    recipe = CATALOG[0]
    other = CATALOG[1]
    monkeypatch.setattr(ws, "CATALOG", (recipe, other))

    monkeypatch.setattr(ws, "load_run_universe", lambda run_dir: [])
    empty = run_wind_scan([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert set(empty) == {recipe.key}
    assert empty[recipe.key]["status"] == "no_universe"
    assert empty[recipe.key]["tradable"] is False

    frames, bench = _bars()

    def fetch(symbols, **kwargs):
        if symbols == ["000300.SH"] or (len(symbols) == 1 and str(symbols[0]).endswith("300")):
            return {symbols[0]: bench}, {}
        return {}, {}

    monkeypatch.setattr(ws, "load_run_universe", lambda run_dir: list(frames))
    monkeypatch.setattr(ws, "_run_source_interval", lambda run_dir: ("local", "1D"))
    monkeypatch.setattr(ws, "_fetch_bars", fetch)
    missing = run_wind_scan([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert missing[recipe.key]["status"] == "no_data"
    assert missing[recipe.key]["tradable"] is False

    def boom(*args, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(ws, "load_run_universe", boom)
    failed = run_wind_scan([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert failed[recipe.key]["status"] == "error"
    assert "disk" in failed[recipe.key]["error"]
    assert failed[recipe.key]["tradable"] is False


def test_run_wind_scan_evaluates_loaded_bars(monkeypatch, tmp_path):
    import src.sector_pick.wind_scan as ws

    recipe = CATALOG[0]
    monkeypatch.setattr(ws, "CATALOG", (recipe,))
    frames, bench = _bars()

    def fetch(symbols, **kwargs):
        if len(symbols) == 1:
            return {symbols[0]: bench}, {}
        return frames, {}

    monkeypatch.setattr(ws, "load_run_universe", lambda run_dir: list(frames))
    monkeypatch.setattr(ws, "_run_source_interval", lambda run_dir: ("local", "1D"))
    monkeypatch.setattr(ws, "_fetch_bars", fetch)
    out = run_wind_scan(None, as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert out[recipe.key]["status"] == "ok"
    assert out[recipe.key]["tradable"] is False
    assert out[recipe.key]["members"] == 4


def test_run_wind_cards_fail_soft_and_skip_bad_factors(monkeypatch, tmp_path):
    import src.sector_pick.wind_scan as ws

    recipe = CATALOG[0]
    monkeypatch.setattr(ws, "CATALOG", (recipe,))
    frames, bench = _bars()

    class Registry:
        def list(self):
            return ["extra", "empty"]

        def compute(self, aid, panel):
            if aid == "extra":
                raise ValueError("skip")
            if aid == "empty":
                return pd.DataFrame()
            return pd.DataFrame({"x": [1.0]})

    monkeypatch.setattr("src.factors.registry.Registry", Registry)
    monkeypatch.setattr(
        "src.sector_pick.factor_card.build_sector_cards",
        lambda *args, **kwargs: stamp_card(recipe.key),
    )

    def no_data(symbols, **kwargs):
        return {}, {}

    monkeypatch.setattr(ws, "load_run_universe", lambda run_dir: list(frames))
    monkeypatch.setattr(ws, "_run_source_interval", lambda run_dir: ("local", "1D"))
    monkeypatch.setattr(ws, "_fetch_bars", no_data)
    empty = run_wind_cards([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert empty[0]["status"] == "no_data"
    assert empty[0]["tradable"] is False
    assert empty[0]["cards"] == []

    def fetch(symbols, **kwargs):
        if len(symbols) == 1:
            return {symbols[0]: bench}, {}
        return frames, {}

    monkeypatch.setattr(ws, "_fetch_bars", fetch)
    scanned = run_wind_cards([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path, max_factors=2)
    assert scanned[0]["sector"] == recipe.key
    assert scanned[0]["tradable"] is False

    def boom(*args, **kwargs):
        raise RuntimeError("cards")

    monkeypatch.setattr(ws, "load_run_universe", boom)
    failed = run_wind_cards([recipe.key], as_of=date(2024, 6, 1), runs_root=tmp_path)
    assert failed[0]["status"] == "error"
    assert failed[0]["tradable"] is False
    assert failed[0]["cards"] == []
