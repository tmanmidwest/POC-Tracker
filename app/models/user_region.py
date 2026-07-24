"""User ↔ region membership.

A row grants one app user access to one region's POCs. An SE carries a
single membership (their home region); a **manager** carries several (every
region they oversee). Admins ignore memberships entirely (they see all regions);
external viewers ignore them too (scoped by ProjectGrant instead). The set of a
user's region ids is the basis for region RBAC — see
``app.services.access.allowed_region_ids``.

Both foreign keys cascade on delete so memberships disappear automatically when
the user or the region is removed. Region deletion is additionally guarded in the
admin UI so a region with members isn't dropped by accident.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.region import Region


class UserRegion(Base, TimestampMixin):
    """Grants one user access to one region."""

    __tablename__ = "user_regions"
    __table_args__ = (
        UniqueConstraint("user_id", "region_id", name="uq_user_region_user_region"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region_id: Mapped[int] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), nullable=False, index=True
    )

    user: Mapped[AppUser] = relationship("AppUser", foreign_keys=[user_id])
    region: Mapped[Region] = relationship("Region", lazy="joined")

    def __repr__(self) -> str:
        return f"<UserRegion user_id={self.user_id} region_id={self.region_id}>"
