"""Use-case library model — the master template list of use cases.

This is purely a source to pick from when building a project. Entries are
*copied* into a project as ProjectUseCase rows; editing a library entry later
never mutates use cases already pulled into a POC.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.feature_type import FeatureType
from app.models.library_set import LibrarySet


class UseCaseLibrary(Base, TimestampMixin):
    """A master/template use case, pickable when building a project."""

    __tablename__ = "use_case_library"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The named library this entry belongs to (exactly one).
    library_set_id: Mapped[int] = mapped_column(
        ForeignKey("library_sets.id"), nullable=False, index=True
    )

    category: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    # Optional suggested reference number; the project copy gets its own.
    default_reference_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_validation: Mapped[str | None] = mapped_column(Text, nullable=True)

    feature_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("feature_types.id"), nullable=True, index=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    feature_type: Mapped[FeatureType | None] = relationship("FeatureType", lazy="joined")
    library_set: Mapped[LibrarySet] = relationship("LibrarySet", lazy="joined")

    def __repr__(self) -> str:
        return f"<UseCaseLibrary category={self.category!r} name={self.name!r}>"
