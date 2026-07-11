"""Password-reset token — a one-time, expiring link to set a new password.

A local user requests a reset (or is locked out); we store a *hashed* single-use
token here (the plaintext is emailed, never stored — same model as invitations
and API keys). Opening the link and setting a new password marks the token used
and clears any lockout. See app.services.password_reset.
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

STATUS_PENDING = "pending"
STATUS_USED = "used"
STATUS_REVOKED = "revoked"


class PasswordResetToken(Base, TimestampMixin):
    """A pending/used/revoked password-reset token for a local account."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 of the emailed token; the plaintext is never stored.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=STATUS_PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[AppUser] = relationship("AppUser", lazy="joined")

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def __repr__(self) -> str:
        return f"<PasswordResetToken user_id={self.user_id} status={self.status}>"
