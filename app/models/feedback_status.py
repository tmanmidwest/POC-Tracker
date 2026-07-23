"""Feedback status lookup table model.

Global list of feedback statuses (e.g. New, Triaged, Planned, In Progress,
Done, Won't Do), managed by admins. The feedback management board groups and
orders submissions by these, so each carries a sort_order. Mirrors TaskStatus.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class FeedbackStatus(Base, TimestampMixin):
    """A feedback status (pickable global list, admin-managed)."""

    __tablename__ = "feedback_statuses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # Drives board column ordering (lower = earlier / leftmost).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Marks a terminal status (Done / Won't Do) for filtering and reporting.
    is_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<FeedbackStatus name={self.name!r} sort={self.sort_order}>"
