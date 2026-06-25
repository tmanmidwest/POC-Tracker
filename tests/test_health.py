"""Smoke tests for the meta endpoints (/, /health, /docs)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_root_redirects_to_ui(client: TestClient) -> None:
    """The root URL redirects to the UI (which then redirects to login if unauthed)."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "/ui/" in response.headers["location"]


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert "version" in body


def test_swagger_docs_accessible(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_openapi_spec_accessible(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"]
    assert spec["info"]["version"]
