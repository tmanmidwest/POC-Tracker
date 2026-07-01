"""Per-user Google account connection for Google Tasks sync.

One row per app user who has connected their Google account. Stores the
Fernet-encrypted **refresh token** (long-lived; access tokens are short-lived and
minted on demand), the id of their dedicated "POC Tracker" task list, and a
status so the UI can prompt a reconnect when the grant is revoked/expired.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# Connection status values.
STATUS_CONNECTED = "connected"
STATUS_NEEDS_REAUTH = "needs_reauth"  # refresh token rejected — user must reconnect


class UserGoogleCredential(Base, TimestampMixin):
    """A user's connected Google account for Tasks sync."""

    __tablename__ = "user_google_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # Fernet-encrypted OAuth refresh token (recoverable — used to mint access tokens).
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Space-separated OAuth scopes granted.
    scopes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The connected Google account's email, shown in the UI ("Connected as …").
    google_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Id of the user's dedicated "POC Tracker" Google Tasks list (synced into).
    tasklist_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=STATUS_CONNECTED
    )
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # High-water mark for pull reconciliation (phase-2 increment 2).
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last sync error message, surfaced in the UI for troubleshooting.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    @property
    def is_connected(self) -> bool:
        return self.status == STATUS_CONNECTED

    def __repr__(self) -> str:
        return f"<UserGoogleCredential user={self.app_user_id} status={self.status}>"
