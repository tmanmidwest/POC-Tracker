"""A milestone blueprint stored inside a POC template.

Like ``PocTemplateTask``, the date is stored as an *offset in days* from the new
project's start date, so it can be re-anchored when the template is applied. A
null offset means "no date". When a POC is created from a template that has
milestones, these are used instead of the global default set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.poc_template import PocTemplate


class PocTemplateMilestone(Base, TimestampMixin):
    """A milestone blueprint belonging to a POC template."""

    __tablename__ = "poc_template_milestones"

    id: Mapped[int] = mapped_column(primary_key=True)

    template_id: Mapped[int] = mapped_column(
        ForeignKey("poc_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Days relative to the project start date — null means no date.
    target_offset_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    template: Mapped[PocTemplate] = relationship(
        "PocTemplate", back_populates="milestones"
    )

    def __repr__(self) -> str:
        return (
            f"<PocTemplateMilestone id={self.id} template_id={self.template_id} "
            f"name={self.name!r}>"
        )
