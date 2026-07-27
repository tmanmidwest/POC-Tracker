"""Feedback comment — an admin's running note on a feedback item.

A feedback item accrues a timeline of comments (triage decisions, status
updates, closure reasons). Only admins author them, but every internal user can
read them (they surface on the read-only "All Feedback" board); external
submitters never see them. Like ``Feedback.submitter``, the author is a nullable
FK plus an ``author_label`` snapshot so a comment survives the account being
removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.app_user import AppUser
    from app.models.feedback import Feedback


class FeedbackComment(Base, TimestampMixin):
    """One comment on a feedback item, newest-relevant as a timeline."""

    __tablename__ = "feedback_comments"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Parent item. CASCADE so comments are removed with the feedback they annotate.
    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("feedback.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Who wrote it. Nullable + ON DELETE SET NULL so the comment survives if the
    # account is removed; ``author_label`` preserves the display name.
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_label: Mapped[str] = mapped_column(String(200), nullable=False)

    body: Mapped[str] = mapped_column(Text, nullable=False)

    feedback: Mapped[Feedback] = relationship("Feedback", back_populates="comments")
    author: Mapped[AppUser | None] = relationship("AppUser", lazy="joined")

    def __repr__(self) -> str:
        return f"<FeedbackComment id={self.id} feedback_id={self.feedback_id}>"
