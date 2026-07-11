"""External-user invitation — a one-time, expiring token to join and view a project.

An admin (or a project's Sales Engineer) invites an external viewer by email. We
create/reuse the external ``AppUser`` and grant it the project, then store a
*hashed* single-use token here (the plaintext is emailed, never stored — same
model as API keys). The invitee opens the link, sets a password, and the invite
is marked accepted.

See docs/INVITATIONS.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.app_user import AppUser
    from app.models.project import Project

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REVOKED = "revoked"


class UserInvite(Base, TimestampMixin):
    """A pending/accepted/revoked invitation for an external viewer."""

    __tablename__ = "user_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The external AppUser this invite provisions/targets.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The project the invite was about (for the email + landing page). The grant
    # itself lives in project_grants; this survives project deletion as SET NULL.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Snapshots captured at invite time (also mirrored onto the AppUser).
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    invited_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # SHA-256 of the emailed token; the plaintext is never stored.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped[AppUser] = relationship(
        "AppUser", foreign_keys=[user_id], lazy="joined"
    )
    project: Mapped[Project | None] = relationship("Project", lazy="joined")
    invited_by: Mapped[AppUser | None] = relationship(
        "AppUser", foreign_keys=[invited_by_user_id]
    )

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def __repr__(self) -> str:
        return f"<UserInvite email={self.email!r} status={self.status}>"
