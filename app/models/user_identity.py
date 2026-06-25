"""Link between a local AppUser and an external OIDC identity.

A user provisioned via SSO has one row here per provider they've signed in
with. The (provider_id, subject) pair is unique — the OIDC `sub` claim is the
stable identifier for a user at a given provider.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.auth_provider import AuthProvider


class UserIdentity(Base, TimestampMixin):
    """An external identity (provider + subject) bound to a local AppUser."""

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider_id", "subject", name="uq_user_identities_provider_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("auth_providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The OIDC `sub` claim — stable, opaque, unique per provider.
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[AppUser] = relationship("AppUser")
    provider: Mapped[AuthProvider] = relationship("AuthProvider")

    def __repr__(self) -> str:
        return f"<UserIdentity provider_id={self.provider_id} subject={self.subject!r}>"
