"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture(autouse=True)
def _reset_env_config():
    """Clear the cached EnvConfig before each test so monkeypatch.setenv works."""
    from src.config.accessor import reset_env_config
    reset_env_config()
    yield
    reset_env_config()


@pytest.fixture(autouse=True)
def _isolated_lake(tmp_path, monkeypatch):
    """Point the data lake at an empty temp dir for every test.

    Scans now read the lake before falling back to a loader, so without this a
    test that stubs only the network would quietly read whatever the developer's
    real ``~/.vibe-trading/lake`` happens to hold — passing or failing by machine.
    Tests that need stored bars set their own root on top of this.
    """
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path / "lake"))
    from src.config.accessor import reset_env_config

    reset_env_config()
    yield
    reset_env_config()
