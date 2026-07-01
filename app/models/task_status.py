"""Task status lookup table model.

Global list of task statuses (e.g. To Do, In Progress, Blocked, Done), managed
by admins and shared across every user's tasks. The task dashboard groups and
orders tasks by these, so each carries a sort_order. Mirrors ProjectStatus.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class TaskStatus(Base, TimestampMixin):
    """A task status (pickable global list, admin-managed)."""

    __tablename__ = "task_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Drives dashboard ordering of the status groups (lower = earlier).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Marks a terminal status (Done / Cancelled) for filtering and reporting.
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<TaskStatus name={self.name!r} sort={self.sort_order}>"
