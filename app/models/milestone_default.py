"""Default milestone set — the standard POC lifecycle, admin-managed.

A global, ordered list of milestone blueprints (name + a day-offset from the
project start) that every new POC is seeded with unless it's created from a
template that carries its own milestones. Editing this list changes what *future*
POCs get; it never touches milestones already on a live project (they're copied,
not linked). Mirrors the other admin lookups.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class MilestoneDefault(Base, TimestampMixin):
    """A milestone blueprint in the global default lifecycle set."""

    __tablename__ = "milestone_defaults"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    # Days from the project start date to the milestone's target. Null = undated.
    target_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<MilestoneDefault name={self.name!r} offset={self.target_offset_days}>"
