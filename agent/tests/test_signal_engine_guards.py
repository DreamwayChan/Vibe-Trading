"""Guards inside a frozen F0 ``signal_engine.py``.

These two defects live in the engine artifact a board is bound to, not in the
monitor: the stop basis (``entry_px``/``peak_px``) and the turnover budget.
Both were fatal-but-silent — the engine kept returning weights, so nothing
downstream could tell a protected book from an unprotected one.

The engine file is loaded from a run directory because runs are the only place
an F0 engine exists; skip when no such run is on disk.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


RUNS_ROOT = Path(__file__).resolve().parents[1] / "runs"


def _load_engine():
    candidates = sorted(RUNS_ROOT.glob("*_H*_cro_*/code/signal_engine.py"))
    if not candidates:
        pytest.skip("no CRO F0 engine on disk")
    spec = importlib.util.spec_from_file_location("cro_signal_engine", candidates[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SignalEngine, candidates[0]


def _panel(codes, closes):
    """Close frame; a ``None`` entry becomes NaN, i.e. a suspension day."""
    frame = pd.DataFrame(closes, columns=codes)
    return frame


def _ranks(frame):
    return frame.rank(axis=1, ascending=False, method="first")


def test_entry_price_survives_a_suspension_gap():
    """A NaN entry price used to disarm every stop for the whole spell.

    ``A`` is picked on a day the rest of the panel trades but it does not
    (suspension). The old engine recorded ``entry_px = NaN``, and every later
    stop comparison against NaN is False, so the name rode a 20% drop with no
    protection. Prices must fall back to the last traded close instead.
    """
    Engine, _ = _load_engine()
    engine = Engine()
    engine.top_n = 1
    engine.band = 2
    engine.min_hold_days = 0
    engine.max_turnover = 0.20

    dates = pd.bdate_range("2026-01-05", periods=10)
    codes = ["A", "B"]
    close = pd.DataFrame(10.0, index=dates, columns=codes)
    close.loc[dates[5]:dates[7], "A"] = float("nan")  # suspended Mon-Wed
    close.loc[dates[8]:, "A"] = 8.0                    # resumes 20% lower

    rank = pd.DataFrame(0.0, index=dates, columns=codes)
    rank["B"] = 1.0
    rank["A"] = 2.0
    confirm = pd.DataFrame(True, index=dates, columns=codes)
    confirm.loc[dates[0], "A"] = False   # not eligible in week 1
    rank.loc[dates[5]:, "A"] = 1.0       # becomes the top pick in week 2
    rank.loc[dates[5]:, "B"] = 3.0       # and B leaves the band

    weights = engine._build_weights(rank, confirm, close, rank)

    assert weights.loc[dates[0], "B"] == 1.0
    assert weights.loc[dates[5], "A"] == 1.0, "entered on the suspension day"
    assert weights.loc[dates[6], "A"] == 1.0, "held through the gap"
    assert weights.loc[dates[8], "A"] == 0.0, "20% drop must trip the 15% stop"


def test_turnover_cap_freezes_a_concentrated_book_by_design():
    """Pin the known constraint, not a desired behaviour.

    The 20% budget is measured on weights, so refilling a one-name book reads
    as 0.5 turnover and is refused forever. A name-based refill channel was
    implemented and measured on this run's own panel: it removes the freeze
    but takes total return from 180.7% to 50.7% and Sharpe from 1.46 to 0.62.
    The concentration is load-bearing, so the cap stays; a diversified variant
    would be a new run that has to pass the gate on its own.
    """
    Engine, _ = _load_engine()
    engine = Engine()
    engine.top_n = 6
    engine.band = 9
    engine.max_turnover = 0.20

    codes = ["A", "B", "C", "D", "E", "F", "G"]
    dates = pd.bdate_range("2026-01-05", periods=2)
    close = pd.DataFrame([[10.0] * len(codes)] * 2, index=dates, columns=codes)
    rank_row = pd.Series({c: i + 1 for i, c in enumerate(codes)}, dtype=float)
    confirm = pd.Series({c: True for c in codes})
    prev_w = pd.Series(0.0, index=codes)
    prev_w["A"] = 1.0

    selected = engine._select_with_turnover_cap(
        rank_row, confirm, {"A"}, {"A": 10.0}, {"A": 10.0}, {"A": 1.0}, {"A": 3},
        close, 0, prev_w, 1.0, None, None,
    )
    assert selected == {"A"}, (
        "if this changes, the recipe's return profile changes with it: "
        "re-run and re-check the gate before updating the assertion"
    )
