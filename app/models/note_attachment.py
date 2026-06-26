"""Note attachment model — an uploaded file on a project note.

The file bytes live on disk under <data_dir>/note_attachments; this row holds
the metadata and the stored filename. Multiple attachments per note are allowed
(PDFs, Office docs, images).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.project_note import ProjectNote


class NoteAttachment(Base, TimestampMixin):
    """An uploaded file attached to a project note."""

    __tablename__ = "note_attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_note_id: Mapped[int] = mapped_column(
        ForeignKey("project_notes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Random unique name on disk (under <data_dir>/note_attachments).
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    note: Mapped[ProjectNote] = relationship("ProjectNote", back_populates="attachments")

    def __repr__(self) -> str:
        return f"<NoteAttachment id={self.id} note_id={self.project_note_id}>"
