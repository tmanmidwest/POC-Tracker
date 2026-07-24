"""Role model for dynamic RBAC (the role builder).

A role bundles a set of capabilities (via ``role_capabilities``) and is assigned
to users (via ``user_roles``); a user may hold several roles and their effective
permissions are the union (Phase 3). Replaces the four hardcoded roles.

Three flags carry the protected/seeded semantics (see the role-builder plan):

- ``is_system`` — one of the four seeded defaults (admin/manager/standard/
  external). Can't be deleted, and its ``key``/protected flags can't be edited.
- ``is_superuser`` — implicitly passes every capability check (the Admin role).
  The last-admin guard and "seeded admin stays admin" invariant key off this.
- ``is_external`` — the read-only viewer identity bundle. Kept in sync with
  ``AppUser.is_external`` when assigned; never combined with internal roles.

Capabilities are seeded in Phase 4, not here; this is the model + table only.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.capability import Capability


class Role(Base, TimestampMixin):
    """A named, admin-configurable bundle of capabilities."""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable slug ("admin", "manager", ... then admin-defined). Immutable for
    # system roles so seeds and guards can reference it.
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Drives ordering in the roles list and role pickers (lower = earlier).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # Read-only view of the granted capabilities. Writes go through explicit
    # RoleCapability rows in the service layer (Phase 5), so this stays viewonly
    # to avoid double-managing the association.
    capabilities: Mapped[list[Capability]] = relationship(
        "Capability",
        secondary="role_capabilities",
        viewonly=True,
        lazy="selectin",
        order_by="Capability.sort_order",
    )

    def __repr__(self) -> str:
        return f"<Role key={self.key!r} superuser={self.is_superuser}>"
