"""External-user account expiry: acceptance term, the sweep, warnings, and extend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser, Customer, Project, ProjectGrant, ProjectStatus
from app.models.project_grant import TIER_VIEWER
from app.services.passwords import hash_password


def _admin_login(client: TestClient) -> None:
    from app.config import get_settings

    s = get_settings()
    r = client.post(
        "/ui/login",
        data={"username": s.initial_admin_username, "password": s.initial_admin_password},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def _make_se(email: str = "se@corp.com") -> int:
    db = get_session_factory()()
    try:
        se = AppUser(
            username="se-user", email=email, display_name="Sam SE",
            is_external=False, is_active=True, password_hash=hash_password("x" * 10),
        )
        db.add(se)
        db.commit()
        return se.id
    finally:
        db.close()


def _make_project(se_id: int | None = None, name: str = "Acme POC") -> int:
    db = get_session_factory()()
    try:
        cust = Customer(name=f"Cust {name}")
        db.add(cust)
        db.flush()
        status = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        proj = Project(
            customer_id=cust.id, name=name, status_id=status.id, sales_engineer_id=se_id
        )
        db.add(proj)
        db.commit()
        return proj.id
    finally:
        db.close()


def _make_external(
    email: str, *, expires_at, active: bool = True, warned=None, project_id: int | None = None
) -> int:
    db = get_session_factory()()
    try:
        u = AppUser(
            username=email, email=email, is_external=True, is_active=active,
            password_hash=hash_password("x" * 10),
            expires_at=expires_at, expiry_warning_sent_at=warned,
        )
        db.add(u)
        db.flush()
        if project_id:
            db.add(ProjectGrant(project_id=project_id, user_id=u.id, tier=TIER_VIEWER))
        db.commit()
        return u.id
    finally:
        db.close()


def _mock_email(monkeypatch, *, ready: bool = True):
    from app.services import email

    sent: list[dict] = []

    def _fake(db, *, to, subject, text_body, html_body=None):
        sent.append({"to": to, "subject": subject})

    monkeypatch.setattr(email, "send_email", _fake)
    monkeypatch.setattr(email, "is_ready", lambda db: ready)
    return sent


# ---------------------------------------------------------------------------
# Acceptance sets the term
# ---------------------------------------------------------------------------


def test_accept_invite_sets_expiry(client: TestClient, monkeypatch) -> None:
    from app.services import email, invitations

    monkeypatch.setattr(email, "send_email", lambda *a, **k: None)
    pid = _make_project()
    db = get_session_factory()()
    try:
        project = db.get(Project, pid)
        invite, _token = invitations.create_invite(
            db, email="ext@example.com", project=project, base_url="http://testserver"
        )
        user = invitations.accept_invite(db, invite, password="s3cretpass")
        assert user.expires_at is not None
        # Default term is 60 days.
        delta = user.expires_at_aware - datetime.now(UTC)
        assert timedelta(days=59) < delta < timedelta(days=61)
    finally:
        db.close()


def test_ttl_zero_means_never(client: TestClient) -> None:
    from app.services import external_expiry, system_config

    db = get_session_factory()()
    try:
        system_config.set_external_user_ttl_days(db, 0)
        u = AppUser(username="n@x.com", email="n@x.com", is_external=True)
        external_expiry.set_initial_expiry(u)
        assert u.expires_at is None
    finally:
        system_config.set_external_user_ttl_days(db, 60)  # restore
        db.close()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_expire_due_users_deactivates_only_past_due(client: TestClient) -> None:
    from app.services import external_expiry

    now = datetime.now(UTC)
    past = _make_external("past@x.com", expires_at=now - timedelta(days=1))
    future = _make_external("future@x.com", expires_at=now + timedelta(days=30))

    db = get_session_factory()()
    try:
        n = external_expiry.expire_due_users(db)
        assert n == 1
        assert db.get(AppUser, past).is_active is False
        assert db.get(AppUser, future).is_active is True
    finally:
        db.close()


def test_expiry_warning_emails_se_once(client: TestClient, monkeypatch) -> None:
    from app.services import external_expiry

    sent = _mock_email(monkeypatch, ready=True)
    se_id = _make_se("engineer@corp.com")
    pid = _make_project(se_id=se_id)
    now = datetime.now(UTC)
    uid = _make_external("soon@x.com", expires_at=now + timedelta(days=5), project_id=pid)

    db = get_session_factory()()
    try:
        sent_count = external_expiry.send_expiry_warnings(db)
        assert sent_count == 1
        assert sent and sent[0]["to"] == "engineer@corp.com"
        assert db.get(AppUser, uid).expiry_warning_sent_at is not None
        # A second sweep must not re-warn.
        sent.clear()
        assert external_expiry.send_expiry_warnings(db) == 0
        assert sent == []
    finally:
        db.close()


def test_expiry_warning_skipped_when_smtp_not_ready(client: TestClient, monkeypatch) -> None:
    from app.services import external_expiry

    _mock_email(monkeypatch, ready=False)
    se_id = _make_se()
    pid = _make_project(se_id=se_id)
    now = datetime.now(UTC)
    _make_external("soon2@x.com", expires_at=now + timedelta(days=3), project_id=pid)

    db = get_session_factory()()
    try:
        assert external_expiry.send_expiry_warnings(db) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# resolve_extension
# ---------------------------------------------------------------------------


def test_resolve_extension(client: TestClient) -> None:
    from app.services import external_expiry

    now = datetime.now(UTC)
    by_preset = external_expiry.resolve_extension("30", None)
    assert timedelta(days=29) < (by_preset - now) < timedelta(days=31)

    future = (now + timedelta(days=100)).strftime("%Y-%m-%d")
    by_date = external_expiry.resolve_extension(None, future)
    assert by_date.date().isoformat() == future

    past = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    with pytest.raises(ValueError):
        external_expiry.resolve_extension(None, past)


# ---------------------------------------------------------------------------
# Extend routes
# ---------------------------------------------------------------------------


def test_admin_extend_reactivates_and_clears_warning(client: TestClient) -> None:
    _admin_login(client)
    now = datetime.now(UTC)
    uid = _make_external(
        "expired@x.com", expires_at=now - timedelta(days=1),
        active=False, warned=now - timedelta(days=8),
    )
    r = client.post(
        f"/ui/settings/admin-users/{uid}/extend",
        data={"preset": "90"}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = get_session_factory()()
    try:
        u = db.get(AppUser, uid)
        assert u.is_active is True                       # reactivated
        assert u.expiry_warning_sent_at is None          # flag cleared
        assert u.days_until_expiry > 85                   # ~90 days out
    finally:
        db.close()


def test_se_extend_from_project_panel(client: TestClient) -> None:
    _admin_login(client)  # admin can grant any project
    now = datetime.now(UTC)
    pid = _make_project()
    uid = _make_external("v@x.com", expires_at=now + timedelta(days=2), project_id=pid)

    until = (now + timedelta(days=45)).strftime("%Y-%m-%d")
    r = client.post(
        f"/ui/projects/{pid}/external/{uid}/extend",
        data={"until": until}, follow_redirects=False,
    )
    assert r.status_code == 303
    db = get_session_factory()()
    try:
        assert db.get(AppUser, uid).expires_at_aware.date().isoformat() == until
    finally:
        db.close()

    # A viewer not granted this project can't be extended here.
    other = _make_external("other@x.com", expires_at=now + timedelta(days=2))
    r2 = client.post(
        f"/ui/projects/{pid}/external/{other}/extend",
        data={"preset": "60"}, follow_redirects=False,
    )
    assert r2.status_code == 404
