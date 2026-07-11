"""Self-service /ui/profile: update own email/display name, change own password."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser
from app.services.passwords import hash_password, verify_password


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
    is_external: bool = False,
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
            is_external=is_external,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _get(user_id: int) -> AppUser:
    db = get_session_factory()()
    try:
        return db.get(AppUser, user_id)
    finally:
        db.close()


def test_profile_requires_login(client: TestClient) -> None:
    r = client.get("/ui/profile", follow_redirects=False)
    assert r.status_code == 303
    assert "/ui/login" in r.headers["location"]


def test_standard_user_can_set_own_email(client: TestClient) -> None:
    uid = _make_user("selfserve")  # no email
    _login(client, "selfserve", "password123")

    r = client.post(
        "/ui/profile",
        data={"display_name": "Self Serve", "email": "  Self@Example.COM "},
        follow_redirects=False,
    )
    assert r.status_code == 303
    u = _get(uid)
    assert u.email == "self@example.com"  # normalized
    assert u.display_name == "Self Serve"


def test_email_must_be_unique(client: TestClient) -> None:
    _make_user("owner", email="taken@example.com")
    uid = _make_user("claimant")
    _login(client, "claimant", "password123")

    r = client.post(
        "/ui/profile",
        data={"display_name": "", "email": "TAKEN@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "already in use" in r.text
    assert _get(uid).email is None


def test_blank_email_clears_it(client: TestClient) -> None:
    uid = _make_user("clearme", email="clearme@example.com")
    _login(client, "clearme", "password123")
    client.post(
        "/ui/profile", data={"display_name": "", "email": "  "},
        follow_redirects=False,
    )
    assert _get(uid).email is None


def test_external_user_cannot_change_email(client: TestClient) -> None:
    uid = _make_user("ext", email="ext@example.com", is_external=True)
    _login(client, "ext", "password123")

    # Even if a value is posted, external email is ignored (it's their login id).
    client.post(
        "/ui/profile",
        data={"display_name": "Ext User", "email": "hacked@example.com"},
        follow_redirects=False,
    )
    u = _get(uid)
    assert u.email == "ext@example.com"
    assert u.display_name == "Ext User"  # display name still updates

    # The form renders email read-only (disabled), not an editable field.
    page = client.get("/ui/profile").text
    assert "managed by your host" in page


def test_change_own_password_requires_current(client: TestClient) -> None:
    uid = _make_user("pwuser")
    _login(client, "pwuser", "password123")

    # Wrong current password is rejected.
    r = client.post(
        "/ui/profile/password",
        data={
            "current_password": "wrong",
            "new_password": "newpass456",
            "confirm_password": "newpass456",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "current password is incorrect" in r.text
    assert verify_password("password123", _get(uid).password_hash)

    # Correct current password changes it.
    r = client.post(
        "/ui/profile/password",
        data={
            "current_password": "password123",
            "new_password": "newpass456",
            "confirm_password": "newpass456",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert verify_password("newpass456", _get(uid).password_hash)


def test_change_password_rejects_mismatch_and_short(client: TestClient) -> None:
    _make_user("pwrules")
    _login(client, "pwrules", "password123")

    r = client.post(
        "/ui/profile/password",
        data={
            "current_password": "password123",
            "new_password": "abcdefgh1",
            "confirm_password": "different1",
        },
        follow_redirects=False,
    )
    assert "match" in r.text.lower()

    r = client.post(
        "/ui/profile/password",
        data={
            "current_password": "password123",
            "new_password": "short",
            "confirm_password": "short",
        },
        follow_redirects=False,
    )
    assert "at least 8" in r.text


def test_setting_email_clears_the_banner(client: TestClient) -> None:
    _make_user("banished")
    _login(client, "banished", "password123")
    assert "No email address on your account" in client.get("/ui/dashboard").text

    client.post(
        "/ui/profile", data={"display_name": "", "email": "banished@example.com"},
        follow_redirects=False,
    )
    assert "No email address on your account" not in client.get("/ui/dashboard").text
