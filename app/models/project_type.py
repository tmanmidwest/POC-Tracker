"""Project type lookup table model.

Global list of the kind of engagement a POC project is, e.g. Workshop, POC
Playbook, POC Full Stack. Admin-managed master list; the dashboard groups
projects by these. Listed alphabetically (no sort_order).
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class ProjectType(Base, TimestampMixin):
    """A POC project type (pickable global list)."""

    __tablename__ = "project_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<ProjectType name={self.name!r}>"
