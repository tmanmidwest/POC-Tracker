"""Reconcile the code-defined capability registry into the ``capabilities`` table.

The code registry (``app.services.rbac.capabilities.CAPABILITIES``) is the source
of truth; the DB table is a mirror kept only so ``role_capabilities`` has a real
foreign key to reference and so labels/areas are queryable in SQL. This module
brings the table in line with the code on every startup (called from
``seed_database``), the same idempotent "insert what's missing, refresh what
changed, remove what's gone" shape as the other seeders.

Removing a capability from the code registry deletes its row here, which cascades
to any ``role_capabilities`` grants — those grants would be dead anyway, since
nothing in code would ever check the retired capability. Renames are just a
remove + add of two different keys.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.app_user import AppUser
from app.models.capability import Capability
from app.models.role import Role
from app.models.role_capability import RoleCapability
from app.models.user_role import UserRole
from app.services.rbac.capabilities import CAPABILITIES, CAPABILITY_KEYS
from app.services.rbac.defaults import SYSTEM_ROLES

log = logging.getLogger(__name__)


def reconcile_capabilities(db: Session) -> dict[str, int]:
    """Sync the ``capabilities`` table to the code registry. Idempotent.

    Returns a small counts dict (``inserted``/``updated``/``deleted``) for
    logging/tests. Flushes but does not commit — the caller (``seed_database``)
    owns the transaction, matching the other seeders.
    """
    existing = {row.key: row for row in db.execute(select(Capability)).scalars()}

    inserted = 0
    updated = 0
    for sort_order, cap in enumerate(CAPABILITIES):
        row = existing.get(cap.key)
        if row is None:
            db.add(
                Capability(
                    key=cap.key,
                    area=cap.area,
                    label=cap.label,
                    description=cap.description,
                    sort_order=sort_order,
                )
            )
            inserted += 1
            continue
        # Refresh the denormalized copies if the registry entry changed.
        if (
            row.area != cap.area
            or row.label != cap.label
            or row.description != cap.description
            or row.sort_order != sort_order
        ):
            row.area = cap.area
            row.label = cap.label
            row.description = cap.description
            row.sort_order = sort_order
            updated += 1

    # Drop rows whose capability no longer exists in code (cascades to grants).
    stale = [key for key in existing if key not in CAPABILITY_KEYS]
    deleted = 0
    if stale:
        db.execute(delete(Capability).where(Capability.key.in_(stale)))
        deleted = len(stale)

    if inserted or updated or deleted:
        db.flush()
        log.info(
            "reconciled_capabilities",
            extra={"inserted": inserted, "updated": updated, "deleted": deleted},
        )
    return {"inserted": inserted, "updated": updated, "deleted": deleted}


def seed_system_roles(db: Session) -> int:
    """Create the four seeded system roles if missing. Idempotent.

    A role's default capabilities are granted **only when the role row is first
    created** — never re-applied on later startups — so an admin's edits to the
    manager/SE/external capability sets are respected and a removed default isn't
    silently re-added. (Admin is a superuser and gets no capability rows; the UI
    renders it as "all".) Flushes; the caller owns the commit.

    Returns the number of roles created this call.
    """
    existing = set(db.execute(select(Role.key)).scalars())
    created = 0
    for spec in SYSTEM_ROLES:
        if spec.key in existing:
            continue
        role = Role(
            key=spec.key,
            name=spec.name,
            description=spec.description,
            is_system=True,
            is_superuser=spec.is_superuser,
            is_external=spec.is_external,
            is_active=True,
            sort_order=spec.sort_order,
        )
        db.add(role)
        db.flush()  # assign role.id
        for cap_key in sorted(spec.default_capabilities):
            db.add(RoleCapability(role_id=role.id, capability_key=cap_key))
        created += 1
    if created:
        db.flush()
        log.info("seeded_system_roles", extra={"roles_created": created})
    return created


def backfill_user_roles(db: Session) -> int:
    """Assign each roleless user the system role matching their legacy role.

    One-time per user by construction: only users with **zero** ``user_roles``
    rows are touched, so this never fights a later admin assignment (Phase 5) and
    never re-adds a removed role on restart. The mapping uses ``AppUser.role``
    (admin/manager/standard/external), which already resolves the legacy boolean
    flags by precedence. Flushes; the caller owns the commit.

    Returns the number of assignments created this call.
    """
    role_id_by_key = {r.key: r.id for r in db.execute(select(Role)).scalars()}

    # Users that currently hold no role assignment at all.
    assigned_user_ids = set(db.execute(select(UserRole.user_id).distinct()).scalars())
    created = 0
    for user in db.execute(select(AppUser)).scalars():
        if user.id in assigned_user_ids:
            continue
        role_id = role_id_by_key.get(user.role)
        if role_id is None:
            # Roles not seeded yet — shouldn't happen (seed runs first), skip safely.
            continue
        db.add(UserRole(user_id=user.id, role_id=role_id))
        created += 1
    if created:
        db.flush()
        log.info("backfilled_user_roles", extra={"assignments_created": created})
    return created


def role_assignment_counts(db: Session) -> dict[str, int]:
    """Small summary (roles, grants, assignments) for startup logging/tests."""
    return {
        "roles": db.scalar(select(func.count()).select_from(Role)) or 0,
        "role_capabilities": db.scalar(select(func.count()).select_from(RoleCapability)) or 0,
        "user_roles": db.scalar(select(func.count()).select_from(UserRole)) or 0,
    }
