"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

# Redirect the user home before any test module is imported. Module-level
# constants such as ``api.helpers.ENV_PATH`` are computed from ``Path.home()``
# at import time, so a per-test HOME change would leave them pointing at the
# developer's real ``~/.vibe-trading`` while ``Path.home()`` inside the test
# pointed at the temp dir — a mismatch that has nothing to do with the code
# under test. A session-scoped sandbox keeps imports and assertions agreeing,
# and keeps every test out of the real home directory.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="vibe-trading-test-home-"))
os.environ["HOME"] = str(_SESSION_HOME)
os.environ["USERPROFILE"] = str(_SESSION_HOME)
atexit.register(shutil.rmtree, _SESSION_HOME, True)


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


@pytest.fixture(autouse=True)
def _isolated_runtime_root(tmp_path, monkeypatch):
    """Point every per-run data root at a temp dir for each test.

    The lake alone was not enough: the loader cache, the decision journal and
    the paper book each resolve under a root that a used machine already has
    filled. A loader test that patches only the HTTP client still found
    yesterday's cached bars, so the patch was never called and the assertion on
    the mock failed with ``call_args is None`` — green on a clean machine, red
    on a used one. HOME is redirected once at import time (see above); these
    per-test roots keep tests from sharing state with each other.
    """
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE_ROOT", str(tmp_path / "loader_cache"))
    monkeypatch.setenv("VIBE_TRADING_PAPER_BOOK_ROOT", str(tmp_path / "paper_book"))
    monkeypatch.setenv("VIBE_TRADING_HYPOTHESES_PATH", str(tmp_path / "hypotheses.json"))
    from src.config.accessor import reset_env_config

    reset_env_config()
    yield
    reset_env_config()


# ``fx_small`` lives next to the tests so a scratch script can import it and
# reproduce a monitor decision without going through pytest.
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture()
def ashare_frames():
    """The 5-name panel from ``ashare_fixtures`` (clean / ST / halted / 一字 / 三连板)."""
    import ashare_fixtures

    return ashare_fixtures.build()


@pytest.fixture()
def ashare_as_of() -> str:
    """Last session of ``fx_small``."""
    import ashare_fixtures

    return ashare_fixtures.as_of()
