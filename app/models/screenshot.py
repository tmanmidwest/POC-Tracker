"""Screenshot model — an uploaded reference image on a project use case.

The image bytes live on disk under <data_dir>/screenshots; this row holds the
metadata and the stored filename. Multiple screenshots per use case are allowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.project_use_case import ProjectUseCase


class Screenshot(Base, TimestampMixin):
    """An uploaded screenshot attached to a project use case."""

    __tablename__ = "screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_use_case_id: Mapped[int] = mapped_column(
        ForeignKey("project_use_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Random unique name on disk (under <data_dir>/screenshots).
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caption: Mapped[str | None] = mapped_column(String(500), nullable=True)

    use_case: Mapped[ProjectUseCase] = relationship(
        "ProjectUseCase", back_populates="screenshots"
    )

    def __repr__(self) -> str:
        return f"<Screenshot id={self.id} use_case_id={self.project_use_case_id}>"
