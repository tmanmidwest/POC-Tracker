"""OAuth 2.0 client credentials model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser


class OAuthClient(Base, TimestampMixin):
    """OAuth 2.0 client for the client_credentials grant flow.

    The secret is hashed and only shown at creation time. Revoking the client
    prevents new token issuance but does not invalidate already-issued JWTs
    until they expire naturally.
    """

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    client_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id"), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_lifetime_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600
    )

    created_by: Mapped[AppUser] = relationship("AppUser")

    def __repr__(self) -> str:
        return f"<OAuthClient id={self.id} name={self.name!r} client_id={self.client_id!r}>"
