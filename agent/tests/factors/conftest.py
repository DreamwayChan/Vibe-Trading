"""Test isolation for ``agent/tests/factors/``.

Two responsibilities:

1. **Network kill-switch** — alphas must be pure functions of the input panel.
   We disable all sockets during factor tests via ``pytest-socket``. If the
   package is not installed we skip silently (CI installs it from the
   ``dev`` extra in ``pyproject.toml``).
2. **Import root** — the bundled zoo modules live at ``agent/src/factors/zoo``
   and import each other via the ``src.factors.*`` package path. The pytest
   config in ``pyproject.toml`` already sets ``pythonpath = ["agent"]`` so no
   action is needed here, but we keep this file in the test directory so the
   socket hook is scoped to factors-only.

The per-test toggle below is only needed when the suite runs *with* network
access. When the whole run already passes ``--disable-socket`` there is nothing
to toggle — and toggling actively breaks: pytest-socket resolves
``--allow-hosts`` with ``socket.getaddrinfo`` at the start of every item, so
leaving the guard installed between items makes its own bookkeeping raise
``SocketBlockedError`` in setup and hides the real result of the test.
"""

from __future__ import annotations

try:
    from pytest_socket import disable_socket, enable_socket
except ImportError:  # pragma: no cover - exercised only in stripped envs
    disable_socket = None  # type: ignore[assignment]
    enable_socket = None  # type: ignore[assignment]


def _globally_disabled(config) -> bool:
    """True when the session itself runs with ``--disable-socket``."""
    try:
        return bool(config.getoption("disable_socket"))
    except (ValueError, AttributeError):  # pragma: no cover - plugin absent
        return False


def pytest_runtest_setup(item) -> None:  # noqa: D401 - pytest hook
    """Disable sockets before every factors test (scoped, not global)."""
    if disable_socket is None or _globally_disabled(item.config):
        return
    disable_socket()


def pytest_runtest_teardown(item) -> None:  # noqa: D401 - pytest hook
    """Re-enable sockets after each test so non-factors tests stay untouched."""
    if enable_socket is None or _globally_disabled(item.config):
        return
    enable_socket()
