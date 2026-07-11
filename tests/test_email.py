"""Tests for outbound SMTP config + the email service (Phase 1 of invites)."""

from __future__ import annotations

import smtplib

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser
from app.services.passwords import hash_password


def _configure(**over: object) -> None:
    """Persist an SMTP config (enabled + configured by default)."""
    from app.services import email

    db = get_session_factory()()
    try:
        email.set_config(
            db,
            host=over.get("host", "smtp.test"),
            port=over.get("port", 587),
            security=over.get("security", "starttls"),
            username=over.get("username", "user"),
            password=over.get("password", "secret"),
            from_email=over.get("from_email", "noreply@test.com"),
            from_name=over.get("from_name", "POC Tracker"),
            is_enabled=over.get("is_enabled", True),
        )
    finally:
        db.close()


def _install_fake_smtp(monkeypatch, email_mod):  # type: ignore[no-untyped-def]
    """Replace smtplib.SMTP / SMTP_SSL with recording fakes."""
    made: dict[str, list] = {"SMTP": [], "SMTP_SSL": []}

    def mk(kind: str):
        class _Fake:
            def __init__(self, host, port, timeout=None, context=None):  # type: ignore[no-untyped-def]
                self.host, self.port, self.context = host, port, context
                self.kind = kind
                self.calls: list[str] = []
                self.logged_in: tuple | None = None
                self.sent: list = []
                made[kind].append(self)

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *a):  # type: ignore[no-untyped-def]
                self.calls.append("quit")
                return False

            def ehlo(self):  # type: ignore[no-untyped-def]
                self.calls.append("ehlo")

            def starttls(self, context=None):  # type: ignore[no-untyped-def]
                self.calls.append("starttls")

            def login(self, user, pw):  # type: ignore[no-untyped-def]
                self.logged_in = (user, pw)

            def send_message(self, msg, to_addrs=None):  # type: ignore[no-untyped-def]
                self.sent.append((msg, list(to_addrs) if to_addrs else None))

        return _Fake

    monkeypatch.setattr(email_mod.smtplib, "SMTP", mk("SMTP"))
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", mk("SMTP_SSL"))
    return made


# ---------------------------------------------------------------------------
# Config service
# ---------------------------------------------------------------------------


def test_get_config_creates_singleton_with_defaults(client: TestClient) -> None:
    from app.services import email

    db = get_session_factory()()
    try:
        cfg = email.get_config(db)
        assert cfg.id == 1
        assert cfg.port == 587
        assert cfg.security == "starttls"
        assert cfg.is_enabled is False
        assert cfg.is_configured is False  # no host/from yet
    finally:
        db.close()


def test_set_config_encrypts_password_and_blank_keeps_it(client: TestClient) -> None:
    from app.services import email

    db = get_session_factory()()
    try:
        email.set_config(
            db, host="h", port=25, security="none", username="u", password="s3cret",
            from_email="a@b.com", from_name="N", is_enabled=True,
        )
        cfg = email.get_config(db)
        # Stored encrypted (not plaintext), and recoverable.
        assert cfg.password_encrypted and cfg.password_encrypted != "s3cret"
        from app.services.secret_box import decrypt_secret

        assert decrypt_secret(cfg.password_encrypted) == "s3cret"
        assert cfg.is_configured is True
        first = cfg.password_encrypted

        # A blank password keeps the stored one.
        email.set_config(
            db, host="h", port=25, security="none", username="u", password=None,
            from_email="a@b.com", from_name="N", is_enabled=True,
        )
        assert email.get_config(db).password_encrypted == first
    finally:
        db.close()


def test_is_ready_requires_enabled_and_configured(client: TestClient) -> None:
    from app.services import email

    db = get_session_factory()()
    try:
        assert email.is_ready(db) is False
        _configure(is_enabled=False)
        assert email.is_ready(db) is False  # configured but disabled
        _configure(is_enabled=True)
        assert email.is_ready(db) is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_send_email_starttls_authenticates_and_sends(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services import email

    _configure(security="starttls", username="user", password="secret")
    fakes = _install_fake_smtp(monkeypatch, email)

    db = get_session_factory()()
    try:
        email.send_email(
            db, to="dest@x.com", subject="Hi", text_body="Body",
            html_body="<p>Body</p>",
        )
    finally:
        db.close()

    assert not fakes["SMTP_SSL"], "STARTTLS must not use SMTP_SSL"
    smtp = fakes["SMTP"][-1]
    assert "starttls" in smtp.calls
    assert smtp.logged_in == ("user", "secret")
    msg, to_addrs = smtp.sent[-1]
    assert to_addrs == ["dest@x.com"]
    assert msg["To"] == "dest@x.com"
    assert msg["Subject"] == "Hi"
    # From header carries the friendly name + address.
    assert "POC Tracker" in msg["From"] and "noreply@test.com" in msg["From"]


def test_send_email_ssl_uses_smtp_ssl_no_starttls(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services import email

    _configure(security="ssl", port=465)
    fakes = _install_fake_smtp(monkeypatch, email)

    db = get_session_factory()()
    try:
        email.send_email(db, to="dest@x.com", subject="s", text_body="b")
    finally:
        db.close()

    assert not fakes["SMTP"], "SSL must not use plain SMTP"
    smtp = fakes["SMTP_SSL"][-1]
    assert "starttls" not in smtp.calls
    assert smtp.sent


def test_send_email_requires_config(client: TestClient) -> None:
    from app.services import email

    db = get_session_factory()()
    try:
        with pytest.raises(email.EmailNotConfigured):
            email.send_email(db, to="x@y.com", subject="s", text_body="b")
    finally:
        db.close()


def test_send_email_wraps_smtp_failure(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services import email

    _configure(security="none", username=None, password=None)

    class _Boom:
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def ehlo(self):  # type: ignore[no-untyped-def]
            pass

        def send_message(self, *a, **k):  # type: ignore[no-untyped-def]
            raise smtplib.SMTPException("rejected")

    monkeypatch.setattr(email.smtplib, "SMTP", _Boom)

    db = get_session_factory()()
    try:
        with pytest.raises(email.EmailSendError):
            email.send_email(db, to="x@y.com", subject="s", text_body="b")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Admin settings UI
# ---------------------------------------------------------------------------


def test_email_settings_page_and_save(admin_session: TestClient) -> None:
    page = admin_session.get("/ui/settings/email")
    assert page.status_code == 200
    assert "SMTP host" in page.text

    saved = admin_session.post(
        "/ui/settings/email",
        data={
            "host": "smtp.example.com", "port": "587", "security": "starttls",
            "username": "relay", "from_email": "noreply@example.com",
            "from_name": "POC Tracker", "is_enabled": "1",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "smtp.example.com" in admin_session.get("/ui/settings/email").text

    db = get_session_factory()()
    try:
        from app.services import email

        cfg = email.get_config(db)
        assert cfg.host == "smtp.example.com" and cfg.is_enabled is True
    finally:
        db.close()


def test_enable_requires_host_and_from(admin_session: TestClient) -> None:
    resp = admin_session.post(
        "/ui/settings/email",
        data={"host": "", "from_email": "", "is_enabled": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        from app.services import email

        assert email.get_config(db).is_enabled is False
    finally:
        db.close()


def test_test_email_send_success_and_failure(admin_session: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.services import email

    _configure(is_enabled=True)

    monkeypatch.setattr(email, "_deliver", lambda *a, **k: None)
    ok = admin_session.post(
        "/ui/settings/email/test",
        data={"test_recipient": "me@test.com"},
        follow_redirects=False,
    )
    assert ok.status_code == 303

    def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise email.EmailSendError("nope")

    monkeypatch.setattr(email, "_deliver", _boom)
    fail = admin_session.post(
        "/ui/settings/email/test",
        data={"test_recipient": "me@test.com"},
        follow_redirects=False,
    )
    assert fail.status_code == 303  # handled gracefully (flash), not a 500


def test_email_settings_admin_only(client: TestClient) -> None:
    """A standard (non-admin) user is bounced from the email settings page."""
    db = get_session_factory()()
    try:
        db.add(AppUser(
            username="std_email", password_hash=hash_password("password123"),
            is_active=True, is_admin=False, is_external=False,
        ))
        db.commit()
    finally:
        db.close()
    client.post(
        "/ui/login",
        data={"username": "std_email", "password": "password123"},
        follow_redirects=False,
    )
    resp = client.get("/ui/settings/email", follow_redirects=False)
    assert resp.status_code == 303
    assert "/ui/dashboard" in resp.headers.get("location", "")
