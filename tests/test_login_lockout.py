"""Failed-login lockout + self-service password reset (strict, no auto-unlock)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_session_factory
from app.models import AppUser, PasswordResetToken
from app.services.passwords import hash_password


def _make_local_user(
    username: str,
    *,
    password: str = "password123",
    email: str | None = None,
    is_admin: bool = False,
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


def _get_user(user_id: int) -> AppUser:
    db = get_session_factory()()
    try:
        return db.get(AppUser, user_id)
    finally:
        db.close()


def _api_login(client: TestClient, username: str, password: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/auth/session/login",
        json={"username": username, "password": password},
    )


def _ui_login(client: TestClient, username: str, password: str):  # type: ignore[no-untyped-def]
    return client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _mock_email(monkeypatch):  # type: ignore[no-untyped-def]
    from app.services import email

    sent: list[dict] = []

    def _fake(db, *, to, subject, text_body, html_body=None):  # type: ignore[no-untyped-def]
        sent.append({"to": to, "subject": subject, "text": text_body, "html": html_body})

    monkeypatch.setattr(email, "send_email", _fake)
    return sent


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def test_api_locks_after_three_failures(client: TestClient) -> None:
    uid = _make_local_user("locky")

    # Two wrong attempts: rejected but not yet locked.
    assert _api_login(client, "locky", "wrong").status_code == 401
    assert _api_login(client, "locky", "wrong").status_code == 401
    assert not _get_user(uid).is_locked

    # Third wrong attempt trips the lock.
    assert _api_login(client, "locky", "wrong").status_code == 401
    assert _get_user(uid).is_locked

    # Even the CORRECT password is now refused with 423 Locked.
    r = _api_login(client, "locky", "password123")
    assert r.status_code == 423


def test_ui_locks_after_three_failures(client: TestClient) -> None:
    uid = _make_local_user("uilocky")

    for _ in range(3):
        r = _ui_login(client, "uilocky", "nope")
        assert r.status_code == 200  # re-rendered form, not a redirect
    assert _get_user(uid).is_locked

    # Correct password now shows the locked message instead of signing in.
    r = _ui_login(client, "uilocky", "password123")
    assert r.status_code == 200
    assert "locked" in r.text.lower()


def test_successful_login_resets_the_counter(client: TestClient) -> None:
    uid = _make_local_user("resetter")

    assert _api_login(client, "resetter", "wrong").status_code == 401
    assert _api_login(client, "resetter", "wrong").status_code == 401
    assert _get_user(uid).failed_login_count == 2

    # A good login clears the tally, so the next failure starts from zero.
    assert _api_login(client, "resetter", "password123").status_code == 200
    assert _get_user(uid).failed_login_count == 0


def test_seeded_admin_is_lockable(client: TestClient) -> None:
    s = get_settings()
    for _ in range(s.max_login_attempts):
        _api_login(client, s.initial_admin_username, "definitely-wrong")
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(
            AppUser.username == s.initial_admin_username
        ).one()
        assert admin.is_locked
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Admin / CLI recovery
# ---------------------------------------------------------------------------


def test_admin_unlock_restores_access(admin_session: TestClient) -> None:
    client = admin_session
    uid = _make_local_user("unlockme")
    for _ in range(3):
        _api_login(client, "unlockme", "wrong")
    assert _get_user(uid).is_locked

    # Admin lifts the lock from the Users page.
    r = client.post(f"/ui/settings/admin-users/{uid}/unlock", follow_redirects=False)
    assert r.status_code == 303
    assert not _get_user(uid).is_locked

    # The account can sign in again with its real password.
    assert _api_login(client, "unlockme", "password123").status_code == 200


def test_admin_password_change_clears_lockout(admin_session: TestClient) -> None:
    client = admin_session
    uid = _make_local_user("pwchange")
    for _ in range(3):
        _api_login(client, "pwchange", "wrong")
    assert _get_user(uid).is_locked

    client.post(
        f"/ui/settings/admin-users/{uid}/password",
        data={"new_password": "brandnewpw1", "confirm_password": "brandnewpw1"},
        follow_redirects=False,
    )
    assert not _get_user(uid).is_locked
    assert _api_login(client, "pwchange", "brandnewpw1").status_code == 200


def test_cli_reset_admin_clears_lockout(client: TestClient) -> None:
    from app.services.seed_data import reset_admin_password

    s = get_settings()
    for _ in range(s.max_login_attempts):
        _api_login(client, s.initial_admin_username, "wrong")

    db = get_session_factory()()
    try:
        assert reset_admin_password(db, s) is True
    finally:
        db.close()

    assert _api_login(
        client, s.initial_admin_username, s.initial_admin_password
    ).status_code == 200


# ---------------------------------------------------------------------------
# Password reset flow
# ---------------------------------------------------------------------------


def _extract_reset_token(text: str) -> str:
    m = re.search(r"/reset/([A-Za-z0-9_-]+)", text)
    assert m, f"no reset link in email: {text!r}"
    return m.group(1)


def test_reset_flow_unlocks_and_sets_new_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _mock_email(monkeypatch)
    uid = _make_local_user("forgetful", email="forgetful@example.com")

    # Lock the account.
    for _ in range(3):
        _api_login(client, "forgetful", "wrong")
    assert _get_user(uid).is_locked

    # Request a reset by email; a link is emailed.
    r = client.post(
        "/forgot-password", data={"identifier": "forgetful@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert len(sent) == 1
    token = _extract_reset_token(sent[0]["text"])

    # The reset page is reachable, and setting a new password redirects to login.
    assert client.get(f"/reset/{token}").status_code == 200
    r = client.post(
        f"/reset/{token}",
        data={"password": "freshpass99", "confirm": "freshpass99"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/login"

    # Lock is gone and the new password works; the old one does not.
    assert not _get_user(uid).is_locked
    assert _api_login(client, "forgetful", "freshpass99").status_code == 200


def test_reset_token_is_single_use(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _mock_email(monkeypatch)
    _make_local_user("oneshot", email="oneshot@example.com")
    client.post("/forgot-password", data={"identifier": "oneshot@example.com"})
    token = _extract_reset_token(sent[0]["text"])

    first = client.post(
        f"/reset/{token}", data={"password": "newpass111", "confirm": "newpass111"},
        follow_redirects=False,
    )
    assert first.status_code == 303
    # Reusing the same token now shows the invalid page.
    again = client.get(f"/reset/{token}")
    assert "invalid" in again.text.lower()


def test_reset_password_mismatch_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _mock_email(monkeypatch)
    _make_local_user("mismatch", email="mismatch@example.com")
    client.post("/forgot-password", data={"identifier": "mismatch@example.com"})
    token = _extract_reset_token(sent[0]["text"])

    r = client.post(
        f"/reset/{token}", data={"password": "aaaaaaaa1", "confirm": "bbbbbbbb2"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "don't match" in r.text or "match" in r.text.lower()


def test_forgot_password_does_not_enumerate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _mock_email(monkeypatch)

    # Unknown identifier: same generic response, and no email/token created.
    r = client.post("/forgot-password", data={"identifier": "ghost@nowhere.test"})
    assert r.status_code == 200
    assert sent == []

    # Known user WITHOUT an email: still generic, still no email sent.
    _make_local_user("noemailuser")  # no email
    r = client.post("/forgot-password", data={"identifier": "noemailuser"})
    assert r.status_code == 200
    assert sent == []

    db = get_session_factory()()
    try:
        assert db.query(PasswordResetToken).count() == 0
    finally:
        db.close()


def test_invalid_reset_token_shows_invalid_page(client: TestClient) -> None:
    assert "invalid" in client.get("/reset/not-a-real-token").text.lower()


def test_expired_reset_token_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    sent = _mock_email(monkeypatch)
    _make_local_user("expiry", email="expiry@example.com")
    client.post("/forgot-password", data={"identifier": "expiry@example.com"})
    token = _extract_reset_token(sent[0]["text"])

    # Backdate the token past its expiry.
    db = get_session_factory()()
    try:
        row = db.query(PasswordResetToken).one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert "invalid" in client.get(f"/reset/{token}").text.lower()
    r = client.post(
        f"/reset/{token}", data={"password": "whatever12", "confirm": "whatever12"},
        follow_redirects=False,
    )
    assert "invalid" in r.text.lower()
