"""A-share risk markers on the monitor admission path."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.config.accessor import reset_env_config
from src.data.lake_fetch import FetchStats
from src.hypotheses.journal import DecisionJournal
from tests.journal_fields import FIVE_FIELDS
from src.monitor.admission import evaluate_admission
from src.monitor.ashare_risk import assess_flags, stamp_suggestions
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.monitor.scan import run_monitor_scan


def _rows_from_closes(closes: list[float], *, one_word: bool = False) -> list[dict]:
    rows = []
    previous = None
    for offset, close in enumerate(closes):
        pre = previous if previous is not None else close / 1.1
        if one_word and offset == len(closes) - 1:
            open_ = high = low = close
        else:
            open_ = pre
            high = max(pre, close)
            low = min(pre, close)
        rows.append(
            {
                "date": f"2024-06-{offset + 3:02d}",
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000,
                "pre_close": pre,
                "is_st": False,
                "name": "",
            }
        )
        previous = close
    return rows


def test_limit_up_and_one_word_on_the_main_board() -> None:
    rows = _rows_from_closes([10.0, 11.0], one_word=True)
    flags = assess_flags("600519.SH", rows, as_of="2024-06-04")
    assert "limit_up" in flags
    assert "one_word" in flags
    assert "extreme_board" not in flags


def test_growth_board_ten_percent_day_is_not_a_limit() -> None:
    rows = _rows_from_closes([10.0, 11.0], one_word=True)
    flags = assess_flags("300750.SZ", rows, as_of="2024-06-04")
    assert flags == []


def test_st_five_percent_band_blocks_from_the_name() -> None:
    rows = _rows_from_closes([10.0, 10.5])
    rows[-1]["name"] = "*ST示例"
    flags = assess_flags("600001.SH", rows, as_of="2024-06-04")
    assert "st" in flags
    assert "limit_up" in flags


def test_three_board_streak_is_extreme_sentiment() -> None:
    rows = _rows_from_closes([10.0, 11.0, 12.1, 13.31], one_word=False)
    flags = assess_flags("000001.SZ", rows, as_of="2024-06-06")
    assert "limit_up" in flags
    assert "consecutive_board" in flags
    assert "extreme_board" in flags
    assert "one_word" not in flags


def test_panel_is_st_marks_a_quiet_bar() -> None:
    rows = _rows_from_closes([10.0, 10.1])
    panel = {("600001.SH", "2024-06-04"): {"is_st": True, "limit_up": 10.5, "band_mismatch": False}}
    flags = assess_flags("600001.SH", rows, as_of="2024-06-04", panel=panel)
    assert flags == ["st"]


def test_buy_is_blocked_and_sell_is_not() -> None:
    buy = MonitorSuggestion(
        symbol="600519.SH",
        action="buy",
        reason="signal",
        as_of="2024-06-04",
        tradable=True,
        risk_flags=["limit_up", "one_word", "consecutive_board"],
    )
    sell = MonitorSuggestion(
        symbol="600519.SH",
        action="sell",
        reason="signal",
        as_of="2024-06-04",
        tradable=True,
        risk_flags=["limit_up", "st"],
    )
    _admission, annotated = evaluate_admission(
        run_key="run",
        run_dir=None,
        suggestions=[buy, sell],
        journal=_ClosedJournal(),
    )
    blocked, kept = annotated
    assert blocked.tradable is False
    assert "ashare_limit_up" in blocked.block_reasons
    assert "ashare_one_word" in blocked.block_reasons
    assert "ashare_consecutive_board" not in blocked.block_reasons
    assert kept.tradable is False  # no passing gate
    assert "ashare_limit_up" not in kept.block_reasons
    assert kept.risk_flags == ["limit_up", "st"]


class _ClosedJournal:
    """Journal stand-in: no rows, so admission stays on the gate reasons."""

    def latest_for(self, _run_key: str):
        return None, {}


def test_provisional_bar_is_not_stamped() -> None:
    index = pd.to_datetime(["2024-06-03", "2024-06-04"])
    frame = pd.DataFrame(
        {"open": [10.0, 11.0], "high": [10.2, 11.0], "low": [9.8, 11.0], "close": [10.0, 11.0], "volume": [1e6, 1e6]},
        index=index,
    )
    suggestion = MonitorSuggestion(
        symbol="600519.SH",
        action="buy",
        reason="x",
        as_of="2024-06-04",
        provisional=True,
    )
    stamp_suggestions([suggestion], {"600519.SH": frame}, as_of="2024-06-04", panel={})
    assert suggestion.risk_flags == []


def test_stamp_round_trips_on_a_frame() -> None:
    index = pd.to_datetime(["2024-06-03", "2024-06-04"])
    frame = pd.DataFrame(
        {"open": [10.0, 11.0], "high": [10.2, 11.0], "low": [9.8, 11.0], "close": [10.0, 11.0], "volume": [1e6, 1e6]},
        index=index,
    )
    suggestion = MonitorSuggestion(symbol="600519.sh", action="buy", reason="x", as_of="2024-06-04")
    stamp_suggestions([suggestion], {"600519.SH": frame}, as_of="2024-06-04", panel={})
    assert "limit_up" in suggestion.risk_flags
    assert "one_word" in suggestion.risk_flags
    restored = MonitorSuggestion.from_dict(suggestion.to_dict())
    assert restored.risk_flags == suggestion.risk_flags


_ENGINE = "\n".join(
    [
        "import pandas as pd",
        "class SignalEngine:",
        "    def generate(self, data_map):",
        "        out = {}",
        "        for code, df in data_map.items():",
        "            s = pd.Series(0.0, index=df.index)",
        "            s.iloc[-1] = 1.0",
        "            out[code] = s",
        "        return out",
    ]
)


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    reset_env_config()
    return tmp_path


def test_scan_keeps_the_pool_and_blocks_the_limit_up_buy(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "20260101_000000_00_limit1"
    runs = isolated_home / "runs"
    run_dir = runs / run_id
    code = run_dir / "code"
    code.mkdir(parents=True)
    (code / "signal_engine.py").write_text(_ENGINE, encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps({"source": "local", "interval": "1D", "codes": ["600519.SH"]}),
        encoding="utf-8",
    )
    artifacts = run_dir / "artifacts"
    artifacts.mkdir()
    (artifacts / "validation.json").write_text(
        json.dumps(
            {
                "monte_carlo": {"p_value_sharpe": 0.01},
                "bootstrap": {"ci_lower": 0.3},
                "walk_forward": {"consistency_rate": 0.8},
                "cost_aware": True,
            }
        ),
        encoding="utf-8",
    )
    DecisionJournal().record(
        run_id=run_id,
        thesis="Do not chase a sealed board.",
        invalidation="Exit the next day if it opens below the close.",
        planned_position="2% NAV",
        planned_risk="max loss 0.4% NAV",
        **FIVE_FIELDS,
    )
    index = pd.to_datetime(["2024-06-03", "2024-06-04"])
    frame = pd.DataFrame(
        {
            "open": [10.0, 11.0],
            "high": [10.4, 11.0],
            "low": [9.8, 11.0],
            "close": [10.0, 11.0],
            "volume": [1_000_000, 1_000_000],
        },
        index=index,
    )

    def fake_fetch(symbols, *, start, end, source, interval):
        return (
            {symbol: frame.copy() for symbol in symbols},
            {},
            FetchStats(requested=len(symbols), lake_bases=len(symbols)),
        )

    monkeypatch.setattr("src.monitor.scan._fetch_universe_lake_first", fake_fetch)
    monkeypatch.setattr("src.monitor.live_bar.fetch_live_quotes", lambda _symbols: {})
    result = run_monitor_scan(
        MonitorConfig(symbols=["600519.SH"], strategy=StrategyRef(type="run_dir", run_id=run_id)),
        as_of=date(2024, 6, 4),
        runs_root=runs,
    )
    assert result.tradable is True
    suggestion = result.suggestions[0]
    assert suggestion.action == "buy"
    assert suggestion.tradable is False
    assert "ashare_limit_up" in suggestion.block_reasons
    assert "ashare_one_word" in suggestion.block_reasons
