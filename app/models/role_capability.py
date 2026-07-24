"""Role ↔ capability grant (the role → permission map).

One row grants one capability to one role. Composite primary key
(role_id, capability_key) makes each grant unique by construction. Both foreign
keys cascade on delete: dropping a role removes its grants, and if a capability
is retired from the code registry the reconciler deletes its row here too.

The ``capability_key`` FK points at ``capabilities.key`` (a reconciled mirror of
the code registry), so a grant can never reference a capability that doesn't
exist in the registry.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RoleCapability(Base):
    """Grants one capability to one role."""

    __tablename__ = "role_capabilities"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    capability_key: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("capabilities.key", ondelete="CASCADE"),
        primary_key=True,
    )

    def __repr__(self) -> str:
        return f"<RoleCapability role_id={self.role_id} cap={self.capability_key!r}>"
