"""User ↔ role assignment.

One row assigns one role to one app user. A user may hold several roles; their
effective capabilities are the union across them (Phase 3). Composite primary key
(user_id, role_id) prevents duplicate assignments. Both foreign keys cascade on
delete so assignments disappear with the user or the role.

Coexists with ``user_regions`` (the orthogonal region axis) — capabilities answer
*what actions*, regions answer *which projects*; neither replaces the other.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UserRole(Base):
    """Assigns one role to one app user."""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:
        return f"<UserRole user_id={self.user_id} role_id={self.role_id}>"
