"""Regression tests for the SPA deep-link middleware in ``api_server``.

The middleware intercepts browser navigation (``Accept: text/html``) to
SPA pages that share a path with an API endpoint, serving the SPA shell
instead. It must NOT intercept API-only paths even when called with a
text/html accept header — the matcher is intentionally narrow so things
like ``/runs/{id}/code`` and ``/runs/{id}/pine`` keep returning the
correct API response.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse


def _http_request(path: str, accept: str) -> Request:
    """Build a minimal ASGI HTTP request for middleware unit tests."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"accept", accept.encode("latin-1"))],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


class TestSpaHtmlRouteMatcher:
    """Pin the matcher used by ``_spa_html_deep_link_fallback`` middleware."""

    @pytest.mark.parametrize(
        "path",
        [
            "/correlation",        # Correlation page
            "/monitor",            # Monitor board page (shares path with API)
            "/market-pick",        # Market-pick page (shares path with API)
            "/sector-pick",        # Sector-pick page (shares path with API)
            "/runs/abc",           # RunDetail (no trailing slash)
            "/runs/abc-123",       # RunDetail with dashes
            "/runs/abc/",          # RunDetail (trailing slash)
        ],
    )
    def test_spa_pages_match(self, path: str) -> None:
        from api_server import _is_spa_html_route

        assert _is_spa_html_route(path) is True, path

    @pytest.mark.parametrize(
        "path",
        [
            "/runs",                # collection endpoint (API only)
            "/runs/abc/code",       # API-only — must NOT be hijacked
            "/runs/abc/pine",       # API-only — must NOT be hijacked
            "/runs/abc/code/",
            "/runs/abc/foo/bar",    # deeper nested — defensive
            "/sessions/xyz",        # different namespace
            "/api",
            "/skills",
            "/correlation/extra",   # only the bare /correlation page exists
            "/monitor/scan",        # monitor API subpaths stay JSON
            "/monitor/history",
            "/market-pick/scan",    # market-pick API subpaths stay JSON
            "/market-pick/history",
            "/sector-pick/scan",    # sector-pick API subpaths stay JSON
            "/sector-pick/history",
            "/sector-pick/holdings",
        ],
    )
    def test_api_only_paths_do_not_match(self, path: str) -> None:
        from api_server import _is_spa_html_route

        assert _is_spa_html_route(path) is False, path


class TestSpaHtmlDeepLinkFallback:
    """HTML shell for path twins must not be cacheable across Accept values."""

    def test_html_accept_serves_no_store_shell(self, tmp_path, monkeypatch) -> None:
        from src.api import helpers

        (tmp_path / "index.html").write_text("<!doctype html><html></html>\n", encoding="utf-8")
        monkeypatch.setattr(helpers, "_FRONTEND_DIST", tmp_path)

        async def call_next(_request: Request):
            raise AssertionError("API handler must not run for HTML navigation")

        response = asyncio.run(
            helpers._spa_html_deep_link_fallback(
                _http_request("/monitor", "text/html,application/xhtml+xml"),
                call_next,
            )
        )
        assert response.headers.get("cache-control") == "no-store"
        assert response.headers.get("vary") == "Accept"

    def test_json_accept_reaches_api_handler(self, tmp_path, monkeypatch) -> None:
        from src.api import helpers

        (tmp_path / "index.html").write_text("<!doctype html><html></html>\n", encoding="utf-8")
        monkeypatch.setattr(helpers, "_FRONTEND_DIST", tmp_path)

        async def call_next(_request: Request):
            return JSONResponse({"symbols": []})

        response = asyncio.run(
            helpers._spa_html_deep_link_fallback(
                _http_request("/monitor", "application/json"),
                call_next,
            )
        )
        assert response.headers.get("content-type", "").startswith("application/json")
