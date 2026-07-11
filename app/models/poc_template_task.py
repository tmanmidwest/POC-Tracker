"""A task blueprint stored inside a POC template.

Dates are stored as *offsets in days* rather than absolute dates, so they can be
resolved against the new project's start date (or the creation date) when the
template is applied. A null offset means "no date".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.poc_template import PocTemplate


class PocTemplateTask(Base, TimestampMixin):
    """A kickoff-task blueprint belonging to a POC template."""

    __tablename__ = "poc_template_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)

    template_id: Mapped[int] = mapped_column(
        ForeignKey("poc_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Days relative to the project start (or creation date) — null means no date.
    start_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    template: Mapped[PocTemplate] = relationship("PocTemplate", back_populates="tasks")

    def __repr__(self) -> str:
        return f"<PocTemplateTask id={self.id} template_id={self.template_id} title={self.title!r}>"
