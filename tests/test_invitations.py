"""Tests for external-user invitations (Phase 2): service + public accept flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser, Customer, Project, ProjectGrant, ProjectStatus, UserInvite
from app.services.passwords import hash_password

_BASE = "http://testserver"


def _seed_project(name: str = "Acme POC") -> int:
    db = get_session_factory()()
    try:
        cust = Customer(name=f"Cust {name}")
        db.add(cust)
        db.flush()
        status = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        proj = Project(customer_id=cust.id, name=name, status_id=status.id)
        db.add(proj)
        db.commit()
        return proj.id
    finally:
        db.close()


def _mock_email(monkeypatch):  # type: ignore[no-untyped-def]
    """Capture outbound email instead of hitting SMTP. Returns the capture list."""
    from app.services import email

    sent: list[dict] = []

    def _fake(db, *, to, subject, text_body, html_body=None):  # type: ignore[no-untyped-def]
        sent.append({"to": to, "subject": subject, "text": text_body, "html": html_body})

    monkeypatch.setattr(email, "send_email", _fake)
    return sent


def _invite(email_addr: str, *, project_id: int | None = None, **kw):  # type: ignore[no-untyped-def]
    from app.services import invitations

    db = get_session_factory()()
    try:
        project = db.get(Project, project_id) if project_id else None
        invite, token = invitations.create_invite(
            db, email=email_addr, project=project, base_url=_BASE, **kw
        )
        return invite.id, token
    finally:
        db.close()


# ---------------------------------------------------------------------------
# create_invite
# ---------------------------------------------------------------------------


def test_create_invite_provisions_user_grant_and_emails(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent = _mock_email(monkeypatch)
    pid = _seed_project("Grant POC")
    _invite("Viewer@Example.com ", project_id=pid, name="Vic Viewer", company="Globex")

    db = get_session_factory()()
    try:
        user = db.query(AppUser).filter(AppUser.email == "viewer@example.com").one()
        assert user.is_external is True
        assert user.username == "viewer@example.com"  # normalized, used as login id
        assert user.password_hash is None  # not set until accept
        assert user.company == "Globex" and user.display_name == "Vic Viewer"
        # Granted the project.
        assert (
            db.query(ProjectGrant)
            .filter(ProjectGrant.project_id == pid, ProjectGrant.user_id == user.id)
            .count()
            == 1
        )
        # Pending invite recorded.
        inv = db.query(UserInvite).filter(UserInvite.user_id == user.id).one()
        assert inv.status == "pending" and inv.project_id == pid
    finally:
        db.close()

    assert len(sent) == 1 and sent[0]["to"] == "viewer@example.com"
    assert "Grant POC" in sent[0]["subject"]


def test_second_invite_reuses_user_and_grant(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    pid = _seed_project()
    _invite("dup@example.com", project_id=pid)
    _invite("dup@example.com", project_id=pid)

    db = get_session_factory()()
    try:
        users = db.query(AppUser).filter(AppUser.email == "dup@example.com").all()
        assert len(users) == 1
        assert (
            db.query(ProjectGrant)
            .filter(ProjectGrant.user_id == users[0].id, ProjectGrant.project_id == pid)
            .count()
            == 1  # not duplicated
        )
        assert db.query(UserInvite).filter(UserInvite.user_id == users[0].id).count() == 2
    finally:
        db.close()


def test_create_invite_refuses_internal_email(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    db = get_session_factory()()
    try:
        db.add(AppUser(username="insider", email="insider@corp.com", is_external=False))
        db.commit()
        with pytest.raises(invitations.InvitationError):
            invitations.create_invite(db, email="insider@corp.com", base_url=_BASE)
    finally:
        db.close()


def test_create_invite_requires_valid_email(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    db = get_session_factory()()
    try:
        with pytest.raises(invitations.InvitationError):
            invitations.create_invite(db, email="not-an-email", base_url=_BASE)
    finally:
        db.close()


def test_create_invite_requires_base_url(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    db = get_session_factory()()
    try:
        # No base_url arg and POCT_PUBLIC_BASE_URL unset -> can't build the link.
        with pytest.raises(invitations.InvitationError):
            invitations.create_invite(db, email="x@example.com")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# verify / accept / resend / revoke
# ---------------------------------------------------------------------------


def test_verify_token_valid_wrong_and_expired(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    inv_id, token = _invite("v@example.com")
    db = get_session_factory()()
    try:
        assert invitations.verify_token(db, token) is not None
        assert invitations.verify_token(db, "bogus") is None
        # Expire it.
        inv = db.get(UserInvite, inv_id)
        inv.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
        assert invitations.verify_token(db, token) is None
    finally:
        db.close()


def test_accept_sets_password_and_enables_login(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    inv_id, token = _invite("login@example.com")
    db = get_session_factory()()
    try:
        invite = invitations.verify_token(db, token)
        user = invitations.accept_invite(db, invite, password="s3cretpass")
        assert user.password_hash is not None
        assert db.get(UserInvite, inv_id).status == "accepted"
        # Token is single-use now.
        assert invitations.verify_token(db, token) is None
    finally:
        db.close()

    # They can now log in with their email as the username.
    resp = client.post(
        "/ui/login",
        data={"username": "login@example.com", "password": "s3cretpass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_accept_rejects_short_password(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    _inv_id, token = _invite("short@example.com")
    db = get_session_factory()()
    try:
        invite = invitations.verify_token(db, token)
        with pytest.raises(invitations.InvitationError):
            invitations.accept_invite(db, invite, password="short")
    finally:
        db.close()


def test_resend_rotates_token_and_revoke_kills_it(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    inv_id, token = _invite("rr@example.com")
    db = get_session_factory()()
    try:
        invite = db.get(UserInvite, inv_id)
        new_token = invitations.resend_invite(db, invite, base_url=_BASE)
        assert new_token != token
        assert invitations.verify_token(db, token) is None  # old token dead
        assert invitations.verify_token(db, new_token) is not None

        invitations.revoke_invite(db, db.get(UserInvite, inv_id))
        assert invitations.verify_token(db, new_token) is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public accept routes (unauthenticated)
# ---------------------------------------------------------------------------


def test_accept_page_valid_and_invalid(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    _inv_id, token = _invite("page@example.com", project_id=_seed_project("Shown POC"))

    ok = client.get(f"/invite/{token}")
    assert ok.status_code == 200
    assert "page@example.com" in ok.text and "Shown POC" in ok.text

    bad = client.get("/invite/not-a-real-token")
    assert bad.status_code == 200
    assert "invalid" in bad.text.lower()


def test_accept_flow_logs_in_and_lands_on_project(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    pid = _seed_project("Landing POC")
    _inv_id, token = _invite("land@example.com", project_id=pid)

    # Password mismatch is rejected in-place (no redirect).
    bad = client.post(
        f"/invite/{token}",
        data={"password": "s3cretpass", "confirm": "different"},
        follow_redirects=False,
    )
    assert bad.status_code == 200 and "match" in bad.text.lower()

    # Correct acceptance logs the user in and lands on their granted project.
    landed = client.post(
        f"/invite/{token}",
        data={"password": "s3cretpass", "confirm": "s3cretpass"},
        follow_redirects=True,
    )
    assert landed.status_code == 200
    assert "Landing POC" in landed.text  # external viewer can see the granted project

    # Token is single-use: a second attempt shows the invalid page.
    again = client.get(f"/invite/{token}")
    assert "invalid" in again.text.lower()


# ---------------------------------------------------------------------------
# Phase 3: invite from the project Share panel + Users-page management
# ---------------------------------------------------------------------------


def test_project_panel_invite_creates_grants_and_emails(admin_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent = _mock_email(monkeypatch)
    pid = _seed_project("Panel POC")

    resp = admin_session.post(
        f"/ui/projects/{pid}/invite",
        data={"email": "Panel@Example.com", "name": "Pat", "company": "Initech"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        u = db.query(AppUser).filter(AppUser.email == "panel@example.com").one()
        assert u.is_external and u.company == "Initech"
        assert (
            db.query(ProjectGrant)
            .filter(ProjectGrant.user_id == u.id, ProjectGrant.project_id == pid)
            .count()
            == 1
        )
        assert db.query(UserInvite).filter(UserInvite.user_id == u.id).count() == 1
    finally:
        db.close()
    assert len(sent) == 1 and sent[0]["to"] == "panel@example.com"


def test_project_panel_invite_forbidden_for_non_sharer(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    pid = _seed_project("Locked POC")
    db = get_session_factory()()
    try:
        db.add(AppUser(
            username="std_inv", password_hash=hash_password("password123"),
            is_active=True, is_admin=False, is_external=False,
        ))
        db.commit()
    finally:
        db.close()
    client.post(
        "/ui/login",
        data={"username": "std_inv", "password": "password123"},
        follow_redirects=False,
    )
    # A standard user who is not the project's SE can't invite on it.
    resp = client.post(
        f"/ui/projects/{pid}/invite",
        data={"email": "x@example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_users_page_shows_external_box_and_resend(admin_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent = _mock_email(monkeypatch)
    pid = _seed_project("Listed POC")
    admin_session.post(
        f"/ui/projects/{pid}/invite",
        data={"email": "listed@example.com", "company": "Umbrella"},
        follow_redirects=False,
    )

    page = admin_session.get("/ui/settings/admin-users")
    assert page.status_code == 200
    assert "External users" in page.text
    assert "listed@example.com" in page.text
    assert "Umbrella" in page.text
    assert "Listed POC" in page.text  # the project they can view
    assert "Invited" in page.text  # pending status badge

    db = get_session_factory()()
    try:
        uid = db.query(AppUser).filter(AppUser.email == "listed@example.com").one().id
    finally:
        db.close()

    resend = admin_session.post(
        f"/ui/settings/admin-users/{uid}/resend-invite", follow_redirects=False
    )
    assert resend.status_code == 303
    assert len(sent) == 2  # original + resend


def test_resend_on_accepted_user_is_noop(admin_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    from app.services import invitations

    _inv_id, token = _invite("acc@example.com", project_id=_seed_project())
    db = get_session_factory()()
    try:
        invite = invitations.verify_token(db, token)
        invitations.accept_invite(db, invite, password="s3cretpass")
        uid = invite.user_id
    finally:
        db.close()

    resp = admin_session.post(
        f"/ui/settings/admin-users/{uid}/resend-invite", follow_redirects=False
    )
    assert resp.status_code == 303  # handled (info flash), not an error/500


def test_remove_external_user_cascades_grant_and_invite(admin_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _mock_email(monkeypatch)
    pid = _seed_project("Remove POC")
    admin_session.post(
        f"/ui/projects/{pid}/invite",
        data={"email": "rm@example.com"},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        uid = db.query(AppUser).filter(AppUser.email == "rm@example.com").one().id
    finally:
        db.close()

    resp = admin_session.post(
        f"/ui/settings/admin-users/{uid}/delete", follow_redirects=False
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        assert db.get(AppUser, uid) is None
        assert db.query(UserInvite).filter(UserInvite.user_id == uid).count() == 0
        assert db.query(ProjectGrant).filter(ProjectGrant.user_id == uid).count() == 0
    finally:
        db.close()
