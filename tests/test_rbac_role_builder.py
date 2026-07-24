"""Phase 5 tests: the admin role builder UI + assignment guards.

HTTP flows run as the seeded admin (who holds role.manage via the legacy admin
tier); the guard invariants are exercised directly against the service.
"""

from __future__ import annotations

import pytest

from app.db import get_session_factory
from app.models import AppUser, Role, RoleCapability, UserRole
from app.services.passwords import hash_password
from app.services.rbac.roles import RoleError, set_user_roles


def _mk_internal(db, username, role_key="standard"):
    u = AppUser(
        username=username,
        password_hash=hash_password("pw123456"),
        is_manager=(role_key == "manager"),
    )
    db.add(u)
    db.flush()
    role = db.query(Role).filter_by(key=role_key).one()
    db.add(UserRole(user_id=u.id, role_id=role.id))
    db.commit()
    return u


# --- HTTP flows (as admin) ---------------------------------------------------


def test_create_edit_delete_role_flow(admin_session):
    # Create
    resp = admin_session.post(
        "/ui/settings/roles/new",
        data={
            "name": "Reviewer",
            "description": "Read + report only",
            "capabilities": ["report.generate", "report.choose_audience"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        role = db.query(Role).filter_by(key="reviewer").one()
        assert not role.is_system and not role.is_superuser
        caps = {rc.capability_key for rc in db.query(RoleCapability).filter_by(role_id=role.id)}
        assert caps == {"report.generate", "report.choose_audience"}
        role_id = role.id
    finally:
        db.close()

    # Edit — change capabilities
    resp = admin_session.post(
        f"/ui/settings/roles/{role_id}/edit",
        data={"name": "Reviewer", "description": "", "capabilities": ["report.generate"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        caps = {rc.capability_key for rc in db.query(RoleCapability).filter_by(role_id=role_id)}
        assert caps == {"report.generate"}
    finally:
        db.close()

    # Delete
    resp = admin_session.post(f"/ui/settings/roles/{role_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        assert db.get(Role, role_id) is None
    finally:
        db.close()


def test_cannot_delete_system_role(admin_session):
    db = get_session_factory()()
    try:
        admin_role_id = db.query(Role).filter_by(key="admin").one().id
    finally:
        db.close()
    admin_session.post(f"/ui/settings/roles/{admin_role_id}/delete", follow_redirects=False)
    db = get_session_factory()()
    try:
        assert db.get(Role, admin_role_id) is not None  # still there
    finally:
        db.close()


def test_non_admin_blocked_from_role_builder(client):
    db = get_session_factory()()
    try:
        _mk_internal(db, "se_rb", "standard")
    finally:
        db.close()
    login = client.post(
        "/api/v1/auth/session/login", json={"username": "se_rb", "password": "pw123456"}
    )
    assert login.status_code == 200
    resp = client.get("/ui/settings/roles", follow_redirects=False)
    assert resp.status_code in (302, 303)  # role.manage denied -> redirect


# --- assignment guards (service) ---------------------------------------------


def test_cannot_change_own_roles(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        with pytest.raises(RoleError, match="your own roles"):
            set_user_roles(db, admin, set(), actor=admin)
    finally:
        db.close()


def test_seeded_admin_must_keep_superuser(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        actor = _mk_internal(db, "actor_su", "standard")
        # Give the actor a superuser role so the escalation guard passes.
        su = db.query(Role).filter_by(key="admin").one()
        db.add(UserRole(user_id=actor.id, role_id=su.id))
        db.commit()
        se_role = db.query(Role).filter_by(key="standard").one()
        with pytest.raises(RoleError, match="seeded admin must keep"):
            set_user_roles(db, admin, {se_role.id}, actor=actor)
    finally:
        db.close()


def test_last_superuser_guard(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        # A non-seeded superuser target, made the *only active* superuser by
        # deactivating the seeded admin (the guard is the backstop for exactly
        # that edge case).
        target = _mk_internal(db, "target_su", "standard")
        su = db.query(Role).filter_by(key="admin").one()
        db.add(UserRole(user_id=target.id, role_id=su.id))
        admin.is_active = False
        db.commit()
        se_role = db.query(Role).filter_by(key="standard").one()
        with pytest.raises(RoleError, match="at least one superuser"):
            set_user_roles(db, target, {se_role.id}, actor=admin)
    finally:
        db.close()


def test_non_superuser_actor_cannot_grant_superuser(client):
    db = get_session_factory()()
    try:
        actor = _mk_internal(db, "mgr_actor", "manager")  # not a superuser
        target = _mk_internal(db, "victim", "standard")
        su_role = db.query(Role).filter_by(key="admin").one()
        with pytest.raises(RoleError, match="superuser role"):
            set_user_roles(db, target, {su_role.id}, actor=actor)
    finally:
        db.close()
