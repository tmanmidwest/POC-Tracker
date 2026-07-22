"""Project milestone — a shared lifecycle checkpoint on a POC.

Unlike tasks (which are private, per-user to-dos), milestones are project-owned
structure everyone on the engagement sees: Kickoff, Success Criteria Agreed,
Mid-point Check, Readout, and so on. They give the POC a schedule and a health
signal — a milestone whose target date has passed while still incomplete makes
the project "off track" (see ``app.services.insights``).

A milestone is *complete* exactly when ``completed_date`` is set; marking it done
stamps today's date (mirroring how a project's ``closed_date`` works), so the
timeline carries real dates for later analysis.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.project import Project


class ProjectMilestone(Base, TimestampMixin):
    """A dated lifecycle checkpoint belonging to a POC project."""

    __tablename__ = "project_milestones"

    id: Mapped[int] = mapped_column(primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # When the milestone is due. Null = undated (shows on the timeline but never
    # counts as overdue).
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Set when the milestone is reached. Presence == complete.
    completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Optional short note (e.g. "Readout booked with the CISO").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Timeline ordering (lower = earlier).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship("Project", back_populates="milestones")

    @property
    def is_complete(self) -> bool:
        return self.completed_date is not None

    def is_overdue(self, today: date | None = None) -> bool:
        """Past its target date and not yet complete."""
        if self.target_date is None or self.is_complete:
            return False
        return self.target_date < (today or date.today())

    def __repr__(self) -> str:
        return (
            f"<ProjectMilestone id={self.id} project_id={self.project_id} "
            f"name={self.name!r}>"
        )
