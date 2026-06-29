"""Project use-case model — a use case attached to a specific POC.

Created either by copying a UseCaseLibrary entry (a snapshot — `source` is
"library" and `library_id` records provenance) or added ad-hoc (`source` is
"custom"). Carries the live per-POC state: reference number, status, comments,
and screenshots. Independent of the library original once created.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.feature_type import FeatureType
from app.models.use_case_status import UseCaseStatus

if TYPE_CHECKING:
    from app.models.library_set import LibrarySet
    from app.models.project import Project
    from app.models.screenshot import Screenshot
    from app.models.use_case_library import UseCaseLibrary

SOURCE_LIBRARY = "library"
SOURCE_CUSTOM = "custom"


class ProjectUseCase(Base, TimestampMixin):
    """A use case being tested within a project."""

    __tablename__ = "project_use_cases"

    id: Mapped[int] = mapped_column(primary_key=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # "library" (snapshot of a library entry) or "custom" (ad-hoc).
    source: Mapped[str] = mapped_column(String(20), nullable=False, default=SOURCE_CUSTOM)
    # Provenance only — NOT a live link. Nulled if the library entry is deleted.
    library_id: Mapped[int | None] = mapped_column(
        ForeignKey("use_case_library.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which named library this snapshot came from (provenance/display only).
    library_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_sets.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Per-project reference number for listing/sorting within a category (e.g. 1.1).
    reference_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    category: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_validation: Mapped[str | None] = mapped_column(Text, nullable=True)

    feature_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("feature_types.id"), nullable=True, index=True
    )
    status_id: Mapped[int] = mapped_column(
        ForeignKey("use_case_statuses.id"), nullable=False, index=True
    )

    comments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional calendar date marking when this use case was completed/last updated.
    completed_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    project: Mapped[Project] = relationship("Project", back_populates="use_cases")
    library: Mapped[UseCaseLibrary | None] = relationship("UseCaseLibrary", lazy="joined")
    library_set: Mapped[LibrarySet | None] = relationship("LibrarySet", lazy="joined")
    feature_type: Mapped[FeatureType | None] = relationship("FeatureType", lazy="joined")
    status: Mapped[UseCaseStatus] = relationship("UseCaseStatus", lazy="joined")
    screenshots: Mapped[list[Screenshot]] = relationship(
        "Screenshot",
        back_populates="use_case",
        cascade="all, delete-orphan",
        order_by="Screenshot.id",
    )

    def __repr__(self) -> str:
        return f"<ProjectUseCase id={self.id} project_id={self.project_id} name={self.name!r}>"
