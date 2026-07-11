"""Top-of-page setup banners: 'no email on account' and 'no SMTP configured'."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session_factory
from app.models import AppUser
from app.services.passwords import hash_password

_EMAIL_BANNER = "No email address on your account"
_SMTP_BANNER = "No outbound email server configured"


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def _make_user(
    username: str,
    *,
    email: str | None = None,
    is_admin: bool = False,
    password: str = "password123",
) -> int:
    db = get_session_factory()()
    try:
        u = AppUser(
            username=username,
            password_hash=hash_password(password),
            email=email,
            is_active=True,
            is_admin=is_admin,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _configure_smtp() -> None:
    from app.services import email as email_service

    db = get_session_factory()()
    try:
        email_service.set_config(
            db,
            host="smtp.example.com",
            port=587,
            security="starttls",
            username=None,
            password=None,
            from_email="from@example.com",
            from_name="Questlog",
            is_enabled=True,
        )
    finally:
        db.close()


def test_seeded_admin_sees_both_banners(client: TestClient) -> None:
    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    page = client.get("/ui/dashboard").text
    # Seeded admin has no email and no SMTP configured out of the box.
    assert _EMAIL_BANNER in page
    assert _SMTP_BANNER in page
    # The email banner links to the self-service profile page.
    assert "/ui/profile" in page


def test_email_banner_clears_once_email_is_set(client: TestClient) -> None:
    _make_user("admin2", email="admin2@example.com", is_admin=True)
    _login(client, "admin2", "password123")
    page = client.get("/ui/dashboard").text
    assert _EMAIL_BANNER not in page
    # SMTP is still unconfigured, so that banner remains.
    assert _SMTP_BANNER in page


def test_standard_user_sees_email_banner_linking_to_profile_and_no_smtp_banner(
    client: TestClient,
) -> None:
    _make_user("stduser", is_admin=False)  # no email
    _login(client, "stduser", "password123")
    page = client.get("/ui/dashboard").text
    assert _EMAIL_BANNER in page
    # Standard users can self-serve via their profile page.
    assert "/ui/profile" in page
    # SMTP banner is admin-only.
    assert _SMTP_BANNER not in page


def test_smtp_banner_gone_once_configured(client: TestClient) -> None:
    _make_user("admin3", email="admin3@example.com", is_admin=True)
    _configure_smtp()
    _login(client, "admin3", "password123")
    page = client.get("/ui/dashboard").text
    assert _SMTP_BANNER not in page
    assert _EMAIL_BANNER not in page


def test_smtp_banner_dismiss_persists_for_session_then_returns_on_relogin(
    client: TestClient,
) -> None:
    _make_user("admin4", email="admin4@example.com", is_admin=True)
    _login(client, "admin4", "password123")
    assert _SMTP_BANNER in client.get("/ui/dashboard").text

    # Dismiss it; it stays hidden for the rest of this session.
    r = client.post("/ui/dismiss-smtp-banner")
    assert r.status_code == 200
    assert _SMTP_BANNER not in client.get("/ui/dashboard").text

    # Signing out and back in re-shows it.
    client.post("/ui/logout", follow_redirects=False)
    _login(client, "admin4", "password123")
    assert _SMTP_BANNER in client.get("/ui/dashboard").text
