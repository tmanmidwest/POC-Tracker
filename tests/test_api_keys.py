"""Tests for API key creation, listing, validation, and revocation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
    """A TestClient with an active admin session."""
    settings = get_settings()
    resp = client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    assert resp.status_code == 200
    return client


def test_create_api_key_returns_full_key_once(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/api/v1/auth/api-keys/", json={"name": "My Test Key"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My Test Key"
    assert body["key"].startswith("poct_")
    assert len(body["key"]) > 20
    assert body["key_prefix"] == body["key"][:14]


def test_list_api_keys_does_not_include_secret(logged_in_client: TestClient) -> None:
    logged_in_client.post("/api/v1/auth/api-keys/", json={"name": "Key 1"})
    logged_in_client.post("/api/v1/auth/api-keys/", json={"name": "Key 2"})

    resp = logged_in_client.get("/api/v1/auth/api-keys/")
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 2
    for key in keys:
        assert "key" not in key
        assert "key_hash" not in key
        assert key["key_prefix"].startswith("poct_")


def test_api_key_unauthenticated_returns_401(client: TestClient) -> None:
    """A fresh client with no session can't create API keys."""
    resp = client.post("/api/v1/auth/api-keys/", json={"name": "Sneaky"})
    assert resp.status_code == 401


def test_revoke_api_key_sets_revoked_at(logged_in_client: TestClient) -> None:
    create_resp = logged_in_client.post(
        "/api/v1/auth/api-keys/", json={"name": "Will Revoke"}
    )
    key_id = create_resp.json()["id"]

    revoke_resp = logged_in_client.post(f"/api/v1/auth/api-keys/{key_id}/revoke")
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["revoked_at"] is not None

    # Revoking a second time is idempotent
    revoke_resp2 = logged_in_client.post(f"/api/v1/auth/api-keys/{key_id}/revoke")
    assert revoke_resp2.status_code == 200
    assert revoke_resp2.json()["revoked_at"] == revoke_resp.json()["revoked_at"]


def test_delete_api_key_removes_it(logged_in_client: TestClient) -> None:
    create_resp = logged_in_client.post(
        "/api/v1/auth/api-keys/", json={"name": "Will Delete"}
    )
    key_id = create_resp.json()["id"]

    delete_resp = logged_in_client.delete(f"/api/v1/auth/api-keys/{key_id}")
    assert delete_resp.status_code == 204

    # Subsequent get returns 404
    get_resp = logged_in_client.get(f"/api/v1/auth/api-keys/{key_id}")
    assert get_resp.status_code == 404


def test_create_api_key_with_expiration(logged_in_client: TestClient) -> None:
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    resp = logged_in_client.post(
        "/api/v1/auth/api-keys/",
        json={"name": "Expiring Key", "expires_at": future},
    )
    assert resp.status_code == 201
    assert resp.json()["expires_at"] is not None
