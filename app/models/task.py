"""Task model — a per-user to-do item, optionally tied to a POC project.

Unlike projects (shared team data), tasks are owned by the user who created them:
each user manages their own list. Statuses and priorities are global, admin-
managed lookups that apply to everyone's tasks. Task details use the same
rich-text pattern as project notes (a plain-text ``details`` for search/export
plus sanitized ``details_html`` for display).

The ``sync_*`` columns are reserved for the phase-2 Google Tasks integration
(a per-user opt-in sync); they are unused in phase 1 but present from the first
migration so enabling sync later needs no schema change.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.task_priority import TaskPriority
from app.models.task_status import TaskStatus

if TYPE_CHECKING:
    from app.models.project import Project


class Task(Base, TimestampMixin):
    """A user-owned task, optionally assigned to a project."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The user who owns this task. Tasks are private to their owner (admins may
    # view all). Deleting the user removes their tasks.
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)

    status_id: Mapped[int] = mapped_column(
        ForeignKey("task_statuses.id"), nullable=False, index=True
    )
    # Optional priority. Null = unprioritized.
    priority_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_priorities.id"), nullable=True, index=True
    )

    # Optional single project this task belongs to. If the project is deleted the
    # task survives, unassigned (ON DELETE SET NULL).
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Neither date is required.
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Task details. ``details`` holds a plain-text rendering (search/export/
    # fallback); ``details_html`` holds sanitized rich-text HTML when present.
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Phase-2 Google Tasks sync (reserved; unused in phase 1) ---
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[AppUser] = relationship("AppUser", lazy="joined")
    status: Mapped[TaskStatus] = relationship("TaskStatus", lazy="joined")
    priority: Mapped[TaskPriority | None] = relationship("TaskPriority", lazy="joined")
    project: Mapped[Project | None] = relationship("Project", back_populates="tasks")

    def __repr__(self) -> str:
        return f"<Task id={self.id} owner_user_id={self.owner_user_id} title={self.title!r}>"
