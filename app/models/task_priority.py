"""Task priority lookup table model.

Global list of task priorities (e.g. Low, Medium, High, Urgent), managed by
admins and shared across every user's tasks. Each carries a sort_order (lower =
higher up the list) and an optional hex color used for its badge in the UI.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class TaskPriority(Base, TimestampMixin):
    """A task priority (pickable global list, admin-managed)."""

    __tablename__ = "task_priorities"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Drives ordering in pickers and dashboard sorting (lower = higher priority).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Optional hex color (e.g. "#dc2626") for the priority badge. Null = neutral.
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<TaskPriority name={self.name!r} sort={self.sort_order}>"
