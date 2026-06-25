"""Project status lookup table model.

Global list of POC project statuses, e.g. Pending Scheduling, Pending Use
Cases, In Progress, Completed, Lost. The dashboard groups and orders projects
by these, so each carries a sort_order.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ProjectStatus(Base, TimestampMixin):
    """A POC project status (pickable global list)."""

    __tablename__ = "project_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Drives dashboard ordering of the status groups (lower = earlier).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Marks a terminal status (Completed / Lost) for filtering and reporting.
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<ProjectStatus name={self.name!r} sort={self.sort_order}>"
