"""Feedback model — a bug report or feature request submitted by any user.

Unlike projects (shared team data), a feedback item is submitted by whoever is
signed in; any internal or external user may submit, but only admins manage the
queue (status, priority, internal notes). Statuses are a global, admin-managed
lookup. The submitter is kept as a nullable FK plus a ``submitter_label``
snapshot so the queue still shows who reported an item even if that account is
later removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.feedback_status import FeedbackStatus

if TYPE_CHECKING:
    from app.models.app_user import AppUser


# Kind of feedback. Fixed code-level enum (not an admin lookup).
FEEDBACK_KINDS: tuple[str, ...] = ("bug", "feature_request")
FEEDBACK_KIND_LABELS: dict[str, str] = {
    "bug": "Bug",
    "feature_request": "Feature request",
}

# Admin-set priority. Fixed code-level enum; None = unprioritized.
FEEDBACK_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
FEEDBACK_PRIORITY_COLORS: dict[str, str] = {
    "low": "#16a34a",
    "medium": "#ca8a04",
    "high": "#ea580c",
    "urgent": "#dc2626",
}


class Feedback(Base, TimestampMixin):
    """A user-submitted bug report or feature request."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Who submitted it. Nullable + ON DELETE SET NULL so the item survives if the
    # account is removed; ``submitter_label`` preserves the display name.
    submitter_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    submitter_label: Mapped[str] = mapped_column(String(200), nullable=False)

    # "bug" | "feature_request".
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    status_id: Mapped[int] = mapped_column(
        ForeignKey("feedback_statuses.id"), nullable=False, index=True
    )
    # "low" | "medium" | "high" | "urgent"; None = unprioritized.
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Internal triage notes, visible only to admins.
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitter: Mapped[AppUser | None] = relationship("AppUser", lazy="joined")
    status: Mapped[FeedbackStatus] = relationship("FeedbackStatus", lazy="joined")

    @property
    def kind_label(self) -> str:
        return FEEDBACK_KIND_LABELS.get(self.kind, self.kind)

    @property
    def priority_color(self) -> str | None:
        return FEEDBACK_PRIORITY_COLORS.get(self.priority) if self.priority else None

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} kind={self.kind!r} title={self.title!r}>"
