"""The NaN entry-price guard must live in every engine source, not one run.

A run's ``code/signal_engine.py`` is a copy of a template that four research
scripts regenerate from ``runs/_research_custom_positive/F0/code/signal_engine.py``
(``ENGINE_F0_SRC``). Patching a single run therefore fixes that run and nothing
else: the next ``_write_signal_engine`` call hands out the bug again. These
tests pin the guard at the source and exercise the suspension scenario against
each source a new run can be generated from.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import io
from pathlib import Path

import pandas as pd
import pytest

AGENT = Path(__file__).resolve().parents[1]

# file, template constant (None when the file itself is the engine),
# rendered through str.format
SOURCES = [
    ("scripts/research_better_than_e.py", "ENGINE_TEMPLATE", True),
    ("scripts/research_custom_positive.py", "ENGINE", True),
    ("scripts/run_c_f0_compare.py", "F0_ENGINE", False),
    ("runs/_research_custom_positive/F0/code/signal_engine.py", None, False),
]

SHADOWS = [
    "scripts/research_copper_confirm_search.py",
    "scripts/research_coking_slowbull_tune.py",
]

FILL = {
    "variant": "test",
    "confirm_ids": "[]",
    "custom_ids": "[]",
    "mode": "soft_s",
    "soft_w": 0.0,
    "blend_w": 0.0,
    "veto_q": 0.0,
}


def _read(rel: str) -> str:
    return io.open(AGENT / rel, encoding="utf-8").read()


def _template_text(rel: str, name: str | None) -> str:
    src = _read(rel)
    if name is None:
        return src
    for node in ast.parse(src).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if name in targets and isinstance(node.value, ast.Constant):
            return node.value.value
    raise AssertionError(f"{rel}: no template constant {name}")


def _load_class(rel: str, name: str | None, fmt: bool):
    text = _template_text(rel, name)
    if fmt:
        text = text.format(**FILL)
    namespace: dict = {}
    exec(compile(text, f"<{rel}>", "exec"), namespace)
    return namespace["SignalEngine"]


def _weights(engine, panel: pd.DataFrame) -> pd.Series:
    """Run ``_build_weights`` with as many args as this engine version takes.

    The rank is supplied rather than derived from ``panel``: the engine reads a
    momentum rank that a suspended name would get as NaN, and a missing rank
    triggers ``band_exit`` before the entry-price guard is ever reached.
    """
    rank = pd.DataFrame({"A": 1.0, "B": 2.0}, index=panel.index)
    confirm = pd.DataFrame(True, index=panel.index, columns=panel.columns)
    n = len(inspect.signature(engine._build_weights).parameters)
    args = [rank, confirm, panel, rank][:n]
    return engine._build_weights(*args)["A"]


def test_every_source_carries_the_entry_price_guard() -> None:
    """A bare ``float(close.iloc[i][c])`` records NaN on a suspension day."""
    # class engines also filter ``selected`` through the helper, hence 4
    expected = [(rel, 4) for rel, _n, _f in SOURCES]
    expected += [(rel, 3) for rel in SHADOWS]
    for rel, calls in expected:
        if rel.startswith("runs/") and not (AGENT / rel).exists():
            continue
        src = _read(rel)
        assert "float(close.iloc[i][c])" not in src, f"{rel}: unguarded entry price"
        assert src.count("_px_at(close, i, c)") == calls, f"{rel}: call sites changed"
        assert "def _px_at(" in src, f"{rel}: helper missing"


def test_templates_still_render_into_valid_python() -> None:
    """The guard is a set comprehension; in a ``.format`` template that is a field.

    ``{{...}}`` is the escaped form. An unescaped ``{c for c in ...}`` makes
    ``.format()`` raise at run-generation time — long after the edit that broke
    it — so compile the rendered text instead of checking the substring.
    """
    for rel, name, _fmt in SOURCES[:2]:
        rendered = _template_text(rel, name).format(**FILL)
        compile(rendered, f"<{rel}>", "exec")
        assert "_px_at(close, i, c)" in rendered


@pytest.mark.parametrize("rel,name,fmt", SOURCES, ids=[s[0].split("/")[-1] for s in SOURCES])
def test_template_engine_stops_a_name_entered_during_suspension(rel: str, name, fmt: bool) -> None:
    """A name entered on a suspension day must still hit the 15% peak stop.

    This is the 600721 shape: the name is picked on a day it does not trade, so
    the old code recorded ``entry_px = NaN``. Later prices are valid again, so
    nothing forces an exit and the position simply rides a 20% drop with every
    stop comparing against NaN and returning False.
    """
    if rel.startswith("runs/") and not (AGENT / rel).exists():
        pytest.skip(f"{rel} is gitignored under agent/runs/ on clean checkouts")
    engine = _load_class(rel, name, fmt)()
    engine.top_n = 1
    engine.band = 2
    engine.min_hold_days = 0
    engine.max_turnover = 0.20

    dates = pd.bdate_range("2026-01-05", periods=10)
    panel = pd.DataFrame(10.0, index=dates, columns=["A", "B"])
    panel.loc[dates[5], "A"] = float("nan")  # suspended on the pick day only
    panel.loc[dates[8]:, "A"] = 8.0  # 20% below the last traded close

    weights = _weights(engine, panel)

    assert weights.loc[dates[5]] == 1.0, f"{rel}: entered on the suspension day"
    assert weights.loc[dates[7]] == 1.0, f"{rel}: held once trading resumed"
    assert weights.loc[dates[8]] == 0.0, f"{rel}: 20% drop must trip the 15% stop"


@pytest.mark.parametrize(
    "module,attr",
    [
        ("scripts.research_copper_confirm_search", "_select_with_turnover_cap"),
        ("scripts.research_coking_slowbull_tune", "_select"),
    ],
)
def test_search_harness_keeps_a_holding_through_a_suspension(module: str, attr: str) -> None:
    """The parameter-search shadow must agree with the engine it is searching for.

    These scripts carry their own copy of the selection loop. If the shadow
    keeps dropping suspended names while the engine holds them, a grid search
    optimises a different strategy than the one that ships.
    """
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # research harnesses pull in the backtest stack
        pytest.skip(f"{module} not importable here: {exc}")

    dates = pd.bdate_range("2026-01-05", periods=4)
    close = pd.DataFrame(10.0, index=dates, columns=["A", "B"])
    close.iloc[2, close.columns.get_loc("A")] = float("nan")  # suspension row
    rank_row = pd.Series({"A": 1.0, "B": 5.0})
    confirm = pd.Series({"A": True, "B": True})
    prev_w = pd.Series({"A": 1.0, "B": 0.0})

    args = [
        rank_row, confirm, {"A"}, {"A": 10.0}, {"A": 10.0}, {"A": 1.0}, {"A": 3},
        close, 2, prev_w, 1.0,
    ]
    if attr == "_select":  # coking's copy also takes the knob set
        args.append(mod.Knobs("test"))

    selected = getattr(mod, attr)(*args)
    assert "A" in selected, f"{module}: dropped a holding only because it did not trade"
