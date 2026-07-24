"""Phase 2 tests: the RBAC schema + capability reconciler.

Covers the four new tables (capabilities / roles / role_capabilities /
user_roles), the startup reconcile that mirrors the code registry into the
``capabilities`` table, and the FK cascade behaviour of the join tables. No
enforcement (``user.can()``) yet — that's Phase 3.

Each test takes the ``client`` fixture so app startup runs migrations + seed
(which reconciles capabilities) against that test's isolated DB.
"""

from __future__ import annotations

from app.db import get_session_factory
from app.models import AppUser, Capability, Role, RoleCapability, UserRole
from app.services.rbac.capabilities import CAPABILITIES, CAPABILITY_KEYS
from app.services.rbac.registry import reconcile_capabilities


def test_capabilities_reconciled_from_registry(client):
    db = get_session_factory()()
    try:
        rows = db.query(Capability).all()
        assert {r.key for r in rows} == set(CAPABILITY_KEYS)
        assert len(rows) == len(CAPABILITIES)
        # Denormalized fields + sort_order mirror the code registry declaration.
        by_key = {r.key: r for r in rows}
        for idx, cap in enumerate(CAPABILITIES):
            row = by_key[cap.key]
            assert row.sort_order == idx
            assert row.area == cap.area
            assert row.label == cap.label
            assert row.description == cap.description
    finally:
        db.close()


def test_reconcile_is_idempotent(client):
    db = get_session_factory()()
    try:
        counts = reconcile_capabilities(db)
        db.commit()
        assert counts == {"inserted": 0, "updated": 0, "deleted": 0}
    finally:
        db.close()


def test_reconcile_fixes_drift(client):
    db = get_session_factory()()
    try:
        # A capability no longer in code, plus a stale label on a real one.
        db.add(Capability(key="ghost.cap", area="X", label="Ghost", description="", sort_order=999))
        db.get(Capability, "project.edit").label = "WRONG"
        db.commit()

        counts = reconcile_capabilities(db)
        db.commit()

        assert counts["deleted"] == 1
        assert counts["updated"] >= 1
        assert db.get(Capability, "ghost.cap") is None
        assert db.get(Capability, "project.edit").label != "WRONG"
    finally:
        db.close()


def test_role_grant_and_assignment_relationships(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        role = Role(
            key="reporter",
            name="Reporter",
            is_system=False,
            is_superuser=False,
            is_external=False,
            is_active=True,
            sort_order=50,
        )
        db.add(role)
        db.flush()
        db.add(RoleCapability(role_id=role.id, capability_key="report.generate"))
        db.add(UserRole(user_id=admin.id, role_id=role.id))
        db.commit()

        db.expire_all()
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        # The seeded admin also carries the backfilled "admin" role (Phase 4).
        assert "reporter" in [r.key for r in admin.roles]
        role = db.query(Role).filter_by(key="reporter").one()
        assert [c.key for c in role.capabilities] == ["report.generate"]
    finally:
        db.close()


def test_deleting_role_cascades_to_grants_and_assignments(client):
    db = get_session_factory()()
    try:
        admin = db.query(AppUser).filter(AppUser.is_seeded.is_(True)).one()
        role = Role(key="temp", name="Temp", sort_order=60)
        db.add(role)
        db.flush()
        db.add(RoleCapability(role_id=role.id, capability_key="audit.view"))
        db.add(UserRole(user_id=admin.id, role_id=role.id))
        db.commit()
        role_id = role.id

        db.delete(role)
        db.commit()

        # DB-level ON DELETE CASCADE clears both join tables.
        assert db.query(RoleCapability).filter_by(role_id=role_id).count() == 0
        assert db.query(UserRole).filter_by(role_id=role_id).count() == 0
        # The user itself is untouched.
        assert db.query(AppUser).filter_by(id=admin.id).count() == 1
    finally:
        db.close()


def test_grant_rejects_unknown_capability_key(client):
    import sqlalchemy.exc

    db = get_session_factory()()
    try:
        role = Role(key="badcap", name="Bad", sort_order=70)
        db.add(role)
        db.flush()
        db.add(RoleCapability(role_id=role.id, capability_key="not.acapability"))
        # FK to capabilities.key must reject a key with no registry row.
        try:
            db.commit()
            raised = False
        except sqlalchemy.exc.IntegrityError:
            raised = True
        assert raised, "expected FK violation for unknown capability_key"
    finally:
        db.rollback()
        db.close()
