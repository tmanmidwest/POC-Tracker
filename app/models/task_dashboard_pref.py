"""Per-user task dashboard preferences.

Stores each user's task-dashboard configuration as a JSON blob: which columns to
show, which statuses to include, sort order, and (for admins) whose tasks to
show. One row per app user. Mirrors DashboardPref but kept separate so task and
project view settings don't clobber each other.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class TaskDashboardPref(Base, TimestampMixin):
    """A single user's saved task-dashboard view configuration."""

    __tablename__ = "task_dashboard_prefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # JSON: {"columns": [...], "status_ids": [...], "sort": "...", "owner": "..."}
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<TaskDashboardPref app_user_id={self.app_user_id}>"
