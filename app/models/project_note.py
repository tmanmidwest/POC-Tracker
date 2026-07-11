"""Project note model — a dated journal entry on a project.

A running log of updates/notes, distinct from the project's single free-text
`notes` field. Each entry carries a user-facing date (defaults to today, but
editable when adding a note for a prior day) and is listed newest-first on the
project page.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.note_attachment import NoteAttachment
    from app.models.project import Project


class ProjectNote(Base, TimestampMixin):
    """A dated note/update entry on a project."""

    __tablename__ = "project_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # User-facing date for the note — defaults to today, editable when adding.
    note_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Plain-text rendering of the note, kept for search/export and as a fallback
    # for older notes saved before rich text. Always populated.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Sanitized rich-text HTML, shown in the UI/PDF when present. Null for
    # legacy plain-text notes, which fall back to ``body``.
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Username of whoever added the note, kept for display. Nullable for safety.
    created_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # Internal-only: when true, hidden from external (viewer) users. Internal
    # users always see it. Defaults to false — notes are shared unless marked.
    is_internal_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    project: Mapped[Project] = relationship("Project", back_populates="note_entries")
    attachments: Mapped[list[NoteAttachment]] = relationship(
        "NoteAttachment",
        back_populates="note",
        cascade="all, delete-orphan",
        order_by="NoteAttachment.id",
    )

    def __repr__(self) -> str:
        return f"<ProjectNote id={self.id} project_id={self.project_id} date={self.note_date}>"
