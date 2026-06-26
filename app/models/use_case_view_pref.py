"""Per-user preferences for the project use-case view.

Stores which use-case fields the user wants displayed and an optional status
filter, as a JSON blob. One row per user (applies to every project page).
Mirrors DashboardPref.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UseCaseViewPref(Base):
    """A user's saved use-case view options (visible fields + status filter)."""

    __tablename__ = "use_case_view_prefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    # JSON: {"fields": ["ref", "feature", ...], "status_filter": "all"|"open"|"<id>"}
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<UseCaseViewPref user={self.app_user_id}>"
