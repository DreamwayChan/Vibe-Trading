"""Route-level contracts for GET /tables/engine and POST /tables/extract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import tables_routes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "")
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


class _Capture:
    """Stand-in for ``extract_tables`` that records its arguments."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, int | None, bool]] = []

    def __call__(
        self,
        file_path: str,
        pages: str = "",
        *,
        max_tables: int | None = None,
        include_csv: bool = True,
    ) -> str:
        self.calls.append((file_path, pages, max_tables, include_csv))
        return json.dumps(self.payload, ensure_ascii=False)


_OK_PAYLOAD = {
    "status": "ok",
    "file": "report.pdf",
    "table_count": 1,
    "tables": [{"id": "t1", "caption": "主要财务指标", "columns": ["项目", "2025E"], "rows": [["营收", "1,234"]]}],
}


def test_extract_resolves_relative_upload_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _Capture(_OK_PAYLOAD)
    monkeypatch.setattr(tables_routes, "extract_tables", capture)

    response = client.post(
        "/tables/extract",
        json={"file_path": "uploads/abc123.pdf", "pages": "1-3", "max_tables": 5},
    )

    assert response.status_code == 200
    assert response.json()["table_count"] == 1
    resolved, pages, max_tables, include_csv = capture.calls[0]
    assert resolved == str(tables_routes.AGENT_DIR / "uploads" / "abc123.pdf")
    assert pages == "1-3"
    assert max_tables == 5
    assert include_csv is True


def test_extract_keeps_absolute_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = _Capture(_OK_PAYLOAD)
    monkeypatch.setattr(tables_routes, "extract_tables", capture)
    target = tmp_path / "annual.pdf"

    response = client.post("/tables/extract", json={"file_path": str(target)})

    assert response.status_code == 200
    assert capture.calls[0][0] == str(target)


def test_extract_surfaces_tool_error_as_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = _Capture({"status": "error", "error": "File not found: nope.pdf"})
    monkeypatch.setattr(tables_routes, "extract_tables", capture)

    response = client.post("/tables/extract", json={"file_path": "uploads/nope.pdf"})

    assert response.status_code == 400
    assert "File not found" in response.json()["detail"]


def test_extract_rejects_empty_path(client: TestClient) -> None:
    response = client.post("/tables/extract", json={"file_path": "   "})
    assert response.status_code == 400
    assert "file_path is required" in response.json()["detail"]


def test_extract_rejects_out_of_range_max_tables(client: TestClient) -> None:
    response = client.post(
        "/tables/extract", json={"file_path": "uploads/a.pdf", "max_tables": 0}
    )
    assert response.status_code == 422


def test_engine_status_reports_engine(client: TestClient) -> None:
    response = client.get("/tables/engine")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "unavailable"}
    assert "available" in body
    assert body["max_pages"] >= 1
