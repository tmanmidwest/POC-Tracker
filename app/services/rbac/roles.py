"""Role-builder service: create/edit/delete roles and assign them to users.

All the write-side logic and guard invariants for the admin role builder live
here so the routes stay thin. Guards raise :class:`RoleError` with a
user-facing message; the routes turn that into a flash.

Invariants enforced (see docs/rbac-role-builder-plan.md §4.3/§6):
- System roles (``is_system``) can't be deleted, renamed to a taken key, or have
  their protected flags (``is_superuser``/``is_external``) changed. A superuser
  role's capabilities are implicitly "all", so its matrix is not editable.
- Custom roles are always created non-superuser and non-external (no privilege
  escalation, no fake external-identity roles from the UI).
- A role still assigned to users can't be deleted.
- Role assignment: never leaves zero superusers, the seeded admin always keeps a
  superuser role, no one edits their own roles, and a non-superuser actor can
  neither grant a superuser role nor grant capabilities they don't themselves
  hold.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.app_user import AppUser
from app.models.role import Role
from app.models.role_capability import RoleCapability
from app.models.user_role import UserRole
from app.services.rbac.capabilities import is_valid_capability

log = logging.getLogger(__name__)


class RoleError(ValueError):
    """A role-builder guard rejected an operation (message is user-facing)."""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "role"


def unique_role_key(db: Session, name: str) -> str:
    """A URL/slug key derived from ``name``, made unique with a numeric suffix."""
    base = _slugify(name)
    existing = set(db.execute(select(Role.key)).scalars())
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _valid_capability_keys(raw: list[str]) -> list[str]:
    """Filter submitted capability keys to the ones that actually exist."""
    return sorted({k for k in raw if is_valid_capability(k)})


def set_role_capabilities(db: Session, role: Role, capability_keys: list[str]) -> None:
    """Replace ``role``'s capability grants with exactly ``capability_keys``.

    No-op for superuser roles (they implicitly hold everything). Unknown keys are
    dropped. Flushes; caller commits.
    """
    if role.is_superuser:
        return
    wanted = set(_valid_capability_keys(capability_keys))
    current = {rc.capability_key for rc in db.query(RoleCapability).filter_by(role_id=role.id)}
    for key in current - wanted:
        db.query(RoleCapability).filter_by(role_id=role.id, capability_key=key).delete()
    for key in wanted - current:
        db.add(RoleCapability(role_id=role.id, capability_key=key))
    db.flush()


def create_role(db: Session, *, name: str, description: str, capability_keys: list[str]) -> Role:
    """Create a custom (non-system, non-superuser, non-external) role."""
    name = (name or "").strip()
    if not name:
        raise RoleError("A role name is required.")
    role = Role(
        key=unique_role_key(db, name),
        name=name[:100],
        description=(description or "").strip() or None,
        is_system=False,
        is_superuser=False,
        is_external=False,
        is_active=True,
        sort_order=100,
    )
    db.add(role)
    db.flush()
    set_role_capabilities(db, role, capability_keys)
    return role


def update_role(
    db: Session, role: Role, *, name: str, description: str, capability_keys: list[str]
) -> None:
    """Update a role's name/description and (unless superuser) its capabilities."""
    name = (name or "").strip()
    if not name:
        raise RoleError("A role name is required.")
    role.name = name[:100]
    role.description = (description or "").strip() or None
    # Superuser roles ignore the matrix (implicitly all); others get the new set.
    set_role_capabilities(db, role, capability_keys)


def delete_role(db: Session, role: Role) -> None:
    """Delete a custom role that isn't assigned to anyone."""
    if role.is_system:
        raise RoleError("System roles can't be deleted.")
    assigned = db.scalar(
        select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
    )
    if assigned:
        raise RoleError(f"This role is assigned to {assigned} user(s). Unassign it first.")
    db.delete(role)


# --- assignment --------------------------------------------------------------


def assignable_roles(db: Session) -> list[Role]:
    """Roles offered on the user-role assignment page (internal roles only).

    Excludes the external-viewer role — external identity is managed through the
    invite/expiry flow, not here.
    """
    return list(
        db.execute(
            select(Role).where(Role.is_external.is_(False)).order_by(Role.sort_order, Role.name)
        ).scalars()
    )


def user_role_ids(db: Session, user: AppUser) -> set[int]:
    return set(db.execute(select(UserRole.role_id).where(UserRole.user_id == user.id)).scalars())


def _superuser_user_ids(db: Session) -> set[int]:
    """Ids of active users holding at least one superuser role."""
    rows = db.execute(
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .join(AppUser, AppUser.id == UserRole.user_id)
        .where(Role.is_superuser.is_(True), AppUser.is_active.is_(True))
    ).scalars()
    return set(rows)


def set_user_roles(db: Session, target: AppUser, role_ids: set[int], *, actor: AppUser) -> None:
    """Reconcile ``target``'s role assignments to exactly ``role_ids``.

    Enforces the assignment guards (see module docstring). Raises
    :class:`RoleError` on a violation; flushes on success (caller commits).
    """
    if target.id == actor.id:
        raise RoleError("You can't change your own roles.")

    roles = list(db.execute(select(Role).where(Role.id.in_(role_ids or {-1}))).scalars())
    found_ids = {r.id for r in roles}
    if found_ids != set(role_ids):
        raise RoleError("One or more selected roles no longer exist.")

    # Escalation guard: a non-superuser actor can neither grant a superuser role
    # nor grant capabilities they don't themselves hold.
    if not actor.is_superuser:
        actor_caps = actor.effective_capabilities()
        for r in roles:
            if r.is_superuser:
                raise RoleError("You can't grant a superuser role.")
            granted = {cap.key for cap in r.capabilities}
            if not granted <= actor_caps:
                raise RoleError(
                    f"You can't grant '{r.name}' — it includes capabilities you " "don't hold."
                )

    # Seeded admin must keep a superuser role.
    if target.is_seeded and not any(r.is_superuser for r in roles):
        raise RoleError("The seeded admin must keep a superuser role.")

    # Never remove the last superuser from the system.
    target_currently_super = any(
        r.is_superuser
        for r in db.execute(
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == target.id)
        ).scalars()
    )
    target_will_be_super = any(r.is_superuser for r in roles)
    if target_currently_super and not target_will_be_super:
        supers = _superuser_user_ids(db)
        if supers <= {target.id}:
            raise RoleError("There must be at least one superuser.")

    current = user_role_ids(db, target)
    wanted = set(found_ids)
    for rid in current - wanted:
        db.query(UserRole).filter_by(user_id=target.id, role_id=rid).delete()
    for rid in wanted - current:
        db.add(UserRole(user_id=target.id, role_id=rid))
    db.flush()


def roles_with_counts(db: Session) -> list[dict]:
    """Roles plus their user-assignment and capability counts, for the list page."""
    assign_counts = dict(
        db.execute(select(UserRole.role_id, func.count()).group_by(UserRole.role_id)).all()
    )
    cap_counts = dict(
        db.execute(
            select(RoleCapability.role_id, func.count()).group_by(RoleCapability.role_id)
        ).all()
    )
    roles = db.execute(select(Role).order_by(Role.sort_order, Role.name)).scalars()
    return [
        {
            "role": r,
            "user_count": assign_counts.get(r.id, 0),
            "capability_count": cap_counts.get(r.id, 0),
        }
        for r in roles
    ]
