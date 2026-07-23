"""Region lookup table model.

Global list of geographic regions (e.g. AMER, EMEA, APAC, or finer-grained
territories) used for role-based access control. An SE belongs to one region and
sees only that region's POCs; a manager may span several. Admin-managed master
list. Carries a sort_order so the admin lists and pickers order predictably.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class Region(Base, TimestampMixin):
    """A geographic region for access scoping (pickable global list)."""

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Drives ordering of region pickers and admin lists (lower = earlier).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Marks the seeded "Unassigned" bucket so it can't be deleted/renamed away.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<Region name={self.name!r} sort={self.sort_order}>"
