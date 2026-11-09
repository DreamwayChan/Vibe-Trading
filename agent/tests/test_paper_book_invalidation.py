"""Price invalidation and decay-over-threshold clear tradable."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.engines.china_a import ChinaAEngine
from src.config.accessor import reset_env_config
from src.hypotheses.journal import DecisionJournal
from src.monitor.models import MonitorConfig, MonitorSuggestion, StrategyRef
from src.paper_book.book import step_day
from src.paper_book.models import PAPER_BOOK_DECAY, PAPER_BOOK_INVALIDATED
from src.paper_book.soak import step_range
from src.strategy_store.models import Artifact, ArtifactStatus, ArtifactType
from src.strategy_store.store import InMemoryStrategyStore
from tests.journal_fields import FIVE_FIELDS

DAY = "2026-01-05"
SYMBOL = "000001.SZ"
RUN = "run_px"


def _bar(close: float) -> pd.Series:
    return pd.Series(
        {
            "open": close,
            "close": close,
            "pre_close": 10.0,
            "volume": 1_000.0,
            "trade_date": pd.Timestamp(DAY),
        }
    )


def test_price_breach_clears_tradable_and_downgrades_the_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_DECISION_JOURNAL_PATH", str(tmp_path / "journal.json"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hyp.json"))
    reset_env_config()
    DecisionJournal().record(
        run_id=RUN,
        symbol=SYMBOL,
        thesis="Gap fade.",
        invalidation="Exit on a close below 10.5.",
        planned_position="2% NAV",
        planned_risk="0.4% NAV",
        invalidation_side="below",
        invalidation_price=10.5,
        **FIVE_FIELDS,
    )
    run_dir = tmp_path / "runs" / RUN
    folder = run_dir / "artifacts"
    folder.mkdir(parents=True)
    pd.DataFrame(
        [{"timestamp": "2026-01-02", "equity": 1_000_000.0}, {"timestamp": DAY, "equity": 1_000_000.0}]
    ).to_csv(folder / "equity.csv", index=False)
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(
        Artifact(
            id="",
            type=ArtifactType.STRATEGY,
            name="px",
            universe="ashare",
            run_dir=str(run_dir),
            status=ArtifactStatus.MONITORING,
            tradable=True,
            validation_gate=json.dumps({"passed": True}),
        )
    )
    config = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=RUN),
        suggestions=[
            MonitorSuggestion(
                symbol=SYMBOL, action="buy", reason="signal", as_of=DAY, tradable=True
            )
        ],
        tradable=True,
    )
    report = step_day(
        as_of=DAY,
        config=config,
        root=tmp_path / "book",
        run_dir=run_dir,
        bars={SYMBOL: _bar(10.0)},
        strategy_store=store,
        threshold=1.0,
        engine=ChinaAEngine({}),
    )
    assert report.downgraded is True
    assert report.downgrade_reason == PAPER_BOOK_INVALIDATED
    assert SYMBOL in report.expectation["invalidated_symbols"]
    assert config.tradable is False
    assert PAPER_BOOK_INVALIDATED in config.tradable_reasons
    artifact = store.get_artifact(artifact_id)
    assert artifact is not None
    assert artifact.tradable is False
    assert artifact.status is ArtifactStatus.MONITORING
    gate = json.loads(artifact.validation_gate or "{}")
    assert gate["paper_book"]["reason"] == PAPER_BOOK_INVALIDATED
    assert gate["paper_book"]["symbols"] == [SYMBOL]


def test_decay_over_threshold_clears_tradable_unless_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.paper_book.soak.edge_decay",
        lambda days, **kwargs: {
            "available": True,
            "decayed": True,
            "decay": 0.8,
            "max_decay": kwargs.get("max_decay", 0.5),
            "reason": "sharpe_decay",
        },
    )
    run_dir = tmp_path / "runs" / RUN
    folder = run_dir / "artifacts"
    folder.mkdir(parents=True)
    pd.DataFrame(
        [
            {"timestamp": "2026-01-02", "equity": 1_000_000.0},
            {"timestamp": DAY, "equity": 1_000_000.0},
        ]
    ).to_csv(folder / "equity.csv", index=False)
    store = InMemoryStrategyStore()
    artifact_id = store.register_artifact(
        Artifact(
            id="",
            type=ArtifactType.STRATEGY,
            name="decay",
            universe="ashare",
            run_dir=str(run_dir),
            status=ArtifactStatus.MONITORING,
            tradable=True,
            validation_gate=json.dumps({"passed": True}),
        )
    )
    config = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=RUN),
        suggestions=[
            MonitorSuggestion(
                symbol=SYMBOL, action="hold", reason="flat", as_of=DAY, tradable=False
            )
        ],
        tradable=True,
    )
    window = step_range(
        start=DAY,
        end=DAY,
        config=config,
        run_dir=run_dir,
        root=tmp_path / "book",
        sessions=[DAY],
        bars_by_day={DAY: {SYMBOL: _bar(10.0)}},
        threshold=1.0,
        strategy_store=store,
        use_snapshots=False,
        engine=ChinaAEngine({}),
    )
    assert window.downgraded is True
    assert window.downgrade_reason == PAPER_BOOK_DECAY
    assert window.decay["cleared_tradable"] is True
    assert config.tradable is False
    assert PAPER_BOOK_DECAY in config.tradable_reasons
    artifact = store.get_artifact(artifact_id)
    assert artifact is not None and artifact.tradable is False
    assert artifact.status is ArtifactStatus.MONITORING

    monkeypatch.setenv("VIBE_TRADING_PAPER_BOOK_DECAY_CLEARS_TRADABLE", "0")
    reset_env_config()
    quiet = MonitorConfig(
        strategy=StrategyRef(type="run_dir", run_id=RUN),
        suggestions=[],
        tradable=True,
    )
    kept = step_range(
        start=DAY,
        end=DAY,
        config=quiet,
        run_dir=run_dir,
        root=tmp_path / "book-off",
        sessions=[DAY],
        bars_by_day={DAY: {SYMBOL: _bar(10.0)}},
        threshold=1.0,
        strategy_store=InMemoryStrategyStore(),
        use_snapshots=False,
        engine=ChinaAEngine({}),
    )
    assert kept.decay["cleared_tradable"] is False
    assert kept.downgraded is False
    assert quiet.tradable is True
