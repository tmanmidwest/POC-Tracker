"""A use case stored inside a POC template.

A point-in-time snapshot of the fields needed to recreate a project use case.
``library_id`` is provenance only (nulled if the library entry is deleted); the
snapshot fields are what actually get copied into a new POC, so a template keeps
working even if the underlying library entry changes or disappears.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.poc_template import PocTemplate

SOURCE_LIBRARY = "library"
SOURCE_CUSTOM = "custom"


class PocTemplateUseCase(Base, TimestampMixin):
    """A use case snapshot belonging to a POC template."""

    __tablename__ = "poc_template_use_cases"

    id: Mapped[int] = mapped_column(primary_key=True)

    template_id: Mapped[int] = mapped_column(
        ForeignKey("poc_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_CUSTOM)
    # Provenance only — nulled if the library entry is deleted.
    library_id: Mapped[int | None] = mapped_column(
        ForeignKey("use_case_library.id", ondelete="SET NULL"), nullable=True
    )

    reference_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str] = mapped_column(String(150), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_validation: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("feature_types.id"), nullable=True
    )

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    template: Mapped[PocTemplate] = relationship("PocTemplate", back_populates="use_cases")

    def __repr__(self) -> str:
        return f"<PocTemplateUseCase id={self.id} template_id={self.template_id} name={self.name!r}>"
