"""Per-user dashboard preferences.

Stores each user's dashboard configuration as a JSON blob: which columns to
show, which statuses to include, and sort order. One row per app user.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class DashboardPref(Base, TimestampMixin):
    """A single user's saved dashboard view configuration."""

    __tablename__ = "dashboard_prefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # JSON: {"columns": [...], "statuses": [...], "sort": "...", "order": "..."}
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DashboardPref app_user_id={self.app_user_id}>"
