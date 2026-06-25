"""Tests for the OAuth 2.0 client_credentials flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def logged_in_client(client: TestClient) -> TestClient:
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


@pytest.fixture
def oauth_credentials(logged_in_client: TestClient) -> dict[str, str]:
    """Create an OAuth client and return its credentials."""
    resp = logged_in_client.post(
        "/api/v1/auth/oauth-clients/",
        json={"name": "Saviynt Test", "token_lifetime_seconds": 3600},
    )
    assert resp.status_code == 201
    body = resp.json()
    return {
        "client_id": body["client_id"],
        "client_secret": body["client_secret"],
        "pk": body["id"],
    }


def test_create_oauth_client_returns_secret_once(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/api/v1/auth/oauth-clients/", json={"name": "Test Client"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Test Client"
    assert body["client_id"].startswith("poct_client_")
    assert len(body["client_secret"]) > 20
    assert body["token_lifetime_seconds"] == 3600  # default


def test_list_oauth_clients_does_not_include_secret(
    logged_in_client: TestClient,
) -> None:
    logged_in_client.post("/api/v1/auth/oauth-clients/", json={"name": "C1"})

    resp = logged_in_client.get("/api/v1/auth/oauth-clients/")
    assert resp.status_code == 200
    clients = resp.json()
    assert len(clients) == 1
    for c in clients:
        assert "client_secret" not in c
        assert "client_secret_hash" not in c


def test_token_endpoint_issues_jwt(
    client: TestClient, oauth_credentials: dict[str, str]
) -> None:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": oauth_credentials["client_id"],
            "client_secret": oauth_credentials["client_secret"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"].count(".") == 2  # JWT has 3 segments


def test_token_endpoint_rejects_wrong_secret(
    client: TestClient, oauth_credentials: dict[str, str]
) -> None:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": oauth_credentials["client_id"],
            "client_secret": "wrong-secret",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


def test_token_endpoint_rejects_unknown_client(client: TestClient) -> None:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "poct_client_doesnotexist",
            "client_secret": "anything",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


def test_token_endpoint_rejects_unsupported_grant(client: TestClient) -> None:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "password",
            "client_id": "anything",
            "client_secret": "anything",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_revoked_oauth_client_cannot_get_token(
    client: TestClient,
    logged_in_client: TestClient,
    oauth_credentials: dict[str, str],
) -> None:
    # Revoke it
    revoke_resp = logged_in_client.post(
        f"/api/v1/auth/oauth-clients/{oauth_credentials['pk']}/revoke"
    )
    assert revoke_resp.status_code == 200

    # Try to get a token — should fail
    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": oauth_credentials["client_id"],
            "client_secret": oauth_credentials["client_secret"],
        },
    )
    assert token_resp.status_code == 401
    assert token_resp.json()["error"] == "invalid_client"


def test_short_token_lifetime_accepted(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/api/v1/auth/oauth-clients/",
        json={"name": "Short-lived", "token_lifetime_seconds": 300},
    )
    assert resp.status_code == 201
    assert resp.json()["token_lifetime_seconds"] == 300


def test_token_lifetime_below_minimum_rejected(logged_in_client: TestClient) -> None:
    resp = logged_in_client.post(
        "/api/v1/auth/oauth-clients/",
        json={"name": "Too short", "token_lifetime_seconds": 30},
    )
    assert resp.status_code == 422
