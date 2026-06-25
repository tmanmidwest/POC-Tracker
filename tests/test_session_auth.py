"""Tests for session-based authentication (web UI login)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings


def test_login_with_correct_credentials_succeeds(client: TestClient) -> None:
    settings = get_settings()
    response = client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == settings.initial_admin_username
    assert body["user_id"] > 0
    # Session cookie should be set
    assert "poct_session" in response.cookies


def test_login_with_wrong_password_fails(client: TestClient) -> None:
    settings = get_settings()
    response = client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": "definitely-wrong",
        },
    )
    assert response.status_code == 401
    assert "Invalid username or password" in response.json()["detail"]


def test_login_with_unknown_user_fails(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/session/login",
        json={"username": "nobody", "password": "whatever"},
    )
    assert response.status_code == 401


def test_session_me_when_not_logged_in(client: TestClient) -> None:
    response = client.get("/api/v1/auth/session/me")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "username": None,
        "user_id": None,
    }


def test_session_me_after_login(client: TestClient) -> None:
    settings = get_settings()
    client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    response = client.get("/api/v1/auth/session/me")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["username"] == settings.initial_admin_username


def test_logout_clears_session(client: TestClient) -> None:
    settings = get_settings()
    client.post(
        "/api/v1/auth/session/login",
        json={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
    )
    # Confirm logged in
    assert client.get("/api/v1/auth/session/me").json()["authenticated"] is True
    # Logout
    response = client.post("/api/v1/auth/session/logout")
    assert response.status_code == 200
    # Confirm logged out
    assert client.get("/api/v1/auth/session/me").json()["authenticated"] is False


def test_protected_endpoint_rejects_unauthenticated_request(client: TestClient) -> None:
    """Calling an admin endpoint without a session returns 401."""
    response = client.get("/api/v1/auth/api-keys/")
    assert response.status_code == 401


def test_login_validation_rejects_empty_username(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/session/login",
        json={"username": "", "password": "anything"},
    )
    # Pydantic validation error
    assert response.status_code == 422
