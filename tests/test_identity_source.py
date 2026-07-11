"""Internal-users table shows each account's identity source (Local vs SSO)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session_factory
from app.models import AppUser, AuthProvider, UserIdentity
from app.services.passwords import hash_password


def _login_admin(client: TestClient) -> None:
    s = get_settings()
    r = client.post(
        "/ui/login",
        data={"username": s.initial_admin_username, "password": s.initial_admin_password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def test_identity_source_column_distinguishes_local_and_sso(client: TestClient) -> None:
    _login_admin(client)

    db = get_session_factory()()
    try:
        # A local (password) standard user.
        db.add(
            AppUser(
                username="localstd",
                password_hash=hash_password("password123"),
                is_active=True,
            )
        )
        # An SSO user: no password, linked to a provider via UserIdentity.
        provider = AuthProvider(
            slug="okta",
            display_name="Okta SSO",
            issuer_url="https://example.okta.com",
            client_id="client-abc",
        )
        db.add(provider)
        db.flush()
        sso_user = AppUser(username="ssouser", password_hash=None, is_active=True)
        db.add(sso_user)
        db.flush()
        db.add(
            UserIdentity(
                user_id=sso_user.id, provider_id=provider.id, subject="sub-123"
            )
        )
        db.commit()
    finally:
        db.close()

    page = client.get("/ui/settings/admin-users").text
    assert "Identity source" in page  # the new column header
    assert "Okta SSO" in page  # SSO user shows its provider
    assert "Local" in page  # local accounts (seeded admin, localstd) show Local
