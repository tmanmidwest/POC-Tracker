"""Phase 4 tests: seeded system roles + user_role backfill.

Verifies the four default roles seed with the right flags/capabilities, that the
seeded admin user is backfilled onto the admin role, and that both seeders are
idempotent and edit-preserving (no restart drift). No enforcement yet (Phase 3).
"""

from __future__ import annotations

from app.db import get_session_factory
from app.models import AppUser, Role, RoleCapability, UserRole
from app.services.rbac.defaults import (
    INTERNAL_CAPABILITIES,
    OPEN_CAPABILITIES,
    SYSTEM_ROLE_KEYS,
)
from app.services.rbac.registry import (
    backfill_user_roles,
    seed_system_roles,
)


def _caps(db, role_key: str) -> set[str]:
    role = db.query(Role).filter_by(key=role_key).one()
    return {rc.capability_key for rc in db.query(RoleCapability).filter_by(role_id=role.id)}


def test_system_roles_seeded_with_flags(client):
    db = get_session_factory()()
    try:
        roles = {r.key: r for r in db.query(Role).all()}
        assert set(roles) == set(SYSTEM_ROLE_KEYS)
        assert all(r.is_system for r in roles.values())
        assert roles["admin"].is_superuser and not roles["admin"].is_external
        assert roles["external"].is_external and not roles["external"].is_superuser
        assert not roles["manager"].is_superuser
        assert not roles["standard"].is_superuser
    finally:
        db.close()


def test_default_capability_sets(client):
    db = get_session_factory()()
    try:
        # Admin is a superuser -> no explicit rows (UI renders it as "all").
        assert _caps(db, "admin") == set()
        # Manager and SE share the internal set; external gets the open set.
        assert _caps(db, "manager") == set(INTERNAL_CAPABILITIES)
        assert _caps(db, "standard") == set(INTERNAL_CAPABILITIES)
        assert _caps(db, "external") == set(OPEN_CAPABILITIES)
        # Sanity: open ⊂ internal, and an admin-tier cap is in neither.
        assert set(OPEN_CAPABILITIES) < set(INTERNAL_CAPABILITIES)
        assert "settings.manage" not in INTERNAL_CAPABILITIES
    finally:
        db.close()


def test_seeded_admin_user_backfilled_to_admin_role(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        role_keys = [r.key for r in admin.roles]
        assert role_keys == ["admin"]
    finally:
        db.close()


def test_seeders_are_idempotent(client):
    db = get_session_factory()()
    try:
        assert seed_system_roles(db) == 0
        assert backfill_user_roles(db) == 0
    finally:
        db.close()


def test_backfill_skips_users_that_already_have_roles(client):
    db = get_session_factory()()
    try:
        # Give a fresh user a non-legacy role manually, then confirm backfill
        # leaves them alone (zero-roles guard => one-time semantics).
        from app.services.passwords import hash_password

        u = AppUser(username="mgr1", password_hash=hash_password("x"), is_manager=True)
        db.add(u)
        db.flush()
        manager = db.query(Role).filter_by(key="manager").one()
        db.add(UserRole(user_id=u.id, role_id=manager.id))
        db.commit()

        created = backfill_user_roles(db)
        db.commit()
        # The manual user already had a role, so backfill doesn't touch it; it may
        # still assign any *other* roleless user, so just assert this user is 1 role.
        assert created == 0
        assert [r.key for r in db.get(AppUser, u.id).roles] == ["manager"]
    finally:
        db.close()


def test_reseed_preserves_admin_capability_edits(client):
    db = get_session_factory()()
    try:
        # Simulate an admin removing a default capability from the manager role.
        manager = db.query(Role).filter_by(key="manager").one()
        db.query(RoleCapability).filter_by(
            role_id=manager.id, capability_key="customer.delete"
        ).delete()
        db.commit()

        # Re-seeding must NOT restore it (create-on-first-only).
        assert seed_system_roles(db) == 0
        db.commit()
        assert "customer.delete" not in _caps(db, "manager")
    finally:
        db.close()
