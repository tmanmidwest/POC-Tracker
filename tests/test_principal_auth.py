"""End-to-end tests for the REST API auth dependency.

These tests use a temporary protected route added at runtime to verify that
both API-key auth and OAuth/JWT auth can successfully authenticate the same
endpoint, with the right principal identification.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services.auth import Principal, get_authenticated_principal


@pytest.fixture
def app_with_test_route() -> FastAPI:
    """Build an app and add a protected test route for verifying auth."""
    test_app = create_app()

    @test_app.get("/test/whoami", tags=["test"])
    def whoami(
        principal: Principal = Depends(get_authenticated_principal),
    ) -> dict[str, str]:
        return {"kind": principal.kind.value, "identifier": principal.identifier}

    return test_app


@pytest.fixture
def test_client(app_with_test_route: FastAPI) -> TestClient:
    with TestClient(app_with_test_route) as c:
        yield c


@pytest.fixture
def logged_in(test_client: TestClient) -> TestClient:
    settings = get_settings()
    resp = test_client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    assert resp.status_code == 200
    return test_client


def test_whoami_rejects_missing_auth(test_client: TestClient) -> None:
    resp = test_client.get("/test/whoami")
    assert resp.status_code == 401
    assert "Missing Authorization" in resp.json()["detail"]


def test_whoami_rejects_unknown_token_format(test_client: TestClient) -> None:
    resp = test_client.get(
        "/test/whoami", headers={"Authorization": "Bearer notavalidtoken"}
    )
    assert resp.status_code == 401


def test_whoami_with_api_key_succeeds(
    test_client: TestClient, logged_in: TestClient
) -> None:
    # Create an API key
    create_resp = logged_in.post(
        "/api/v1/auth/api-keys/", json={"name": "E2E Test Key"}
    )
    full_key = create_resp.json()["key"]
    prefix = create_resp.json()["key_prefix"]

    # Use a FRESH client (no session) — only the bearer header should auth
    with TestClient(test_client.app) as fresh:
        resp = fresh.get(
            "/test/whoami", headers={"Authorization": f"Bearer {full_key}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "api_key"
        assert body["identifier"] == f"api_key:{prefix}"


def test_whoami_with_revoked_api_key_fails(
    test_client: TestClient, logged_in: TestClient
) -> None:
    create_resp = logged_in.post(
        "/api/v1/auth/api-keys/", json={"name": "Revoke Me"}
    )
    full_key = create_resp.json()["key"]
    key_id = create_resp.json()["id"]

    # Revoke
    logged_in.post(f"/api/v1/auth/api-keys/{key_id}/revoke")

    # Try to use the revoked key
    with TestClient(test_client.app) as fresh:
        resp = fresh.get(
            "/test/whoami", headers={"Authorization": f"Bearer {full_key}"}
        )
        assert resp.status_code == 401
        assert "revoked" in resp.json()["detail"].lower()


def test_whoami_with_oauth_jwt_succeeds(
    test_client: TestClient, logged_in: TestClient
) -> None:
    # Create OAuth client
    create_resp = logged_in.post(
        "/api/v1/auth/oauth-clients/", json={"name": "E2E OAuth"}
    )
    creds = create_resp.json()

    # Exchange creds for a JWT
    with TestClient(test_client.app) as fresh:
        token_resp = fresh.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        assert token_resp.status_code == 200
        access_token = token_resp.json()["access_token"]

        # Use the JWT
        resp = fresh.get(
            "/test/whoami", headers={"Authorization": f"Bearer {access_token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "oauth"
        assert body["identifier"] == f"oauth:{creds['client_id']}"


def test_whoami_with_tampered_jwt_fails(
    test_client: TestClient, logged_in: TestClient
) -> None:
    create_resp = logged_in.post(
        "/api/v1/auth/oauth-clients/", json={"name": "Tamper Target"}
    )
    creds = create_resp.json()

    with TestClient(test_client.app) as fresh:
        token_resp = fresh.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
            },
        )
        access_token = token_resp.json()["access_token"]

        # Flip one character in the signature segment
        parts = access_token.split(".")
        bad_signature = parts[2][:-2] + ("AA" if not parts[2].endswith("AA") else "BB")
        tampered = ".".join([parts[0], parts[1], bad_signature])

        resp = fresh.get(
            "/test/whoami", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert resp.status_code == 401


def test_whoami_with_non_bearer_scheme_fails(test_client: TestClient) -> None:
    resp = test_client.get("/test/whoami", headers={"Authorization": "Basic dXNlcjpw"})
    assert resp.status_code == 401


def test_api_key_last_used_is_tracked(
    test_client: TestClient, logged_in: TestClient
) -> None:
    create_resp = logged_in.post(
        "/api/v1/auth/api-keys/", json={"name": "Track Usage"}
    )
    full_key = create_resp.json()["key"]
    key_id = create_resp.json()["id"]

    # Before use
    initial = logged_in.get(f"/api/v1/auth/api-keys/{key_id}").json()
    assert initial["last_used_at"] is None

    # Use it
    with TestClient(test_client.app) as fresh:
        fresh.get("/test/whoami", headers={"Authorization": f"Bearer {full_key}"})

    # After use
    updated = logged_in.get(f"/api/v1/auth/api-keys/{key_id}").json()
    assert updated["last_used_at"] is not None
