"""Single-row Google Tasks integration config (one row, fixed primary key of 1).

Holds the admin-provided Google OAuth **client** credentials the app uses to run
the per-user consent flow for Google Tasks sync. The client secret is encrypted
at rest (Fernet, via app.services.secret_box) — it must be recoverable because we
send it to Google on every token exchange/refresh, so it can't be a one-way hash.

Individual users then connect their own Google account (see UserGoogleCredential);
this row is just the app's identity to Google.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# The singleton row always uses this primary key.
GOOGLE_TASKS_CONFIG_ID = 1


class GoogleTasksConfig(Base, TimestampMixin):
    """Google Tasks OAuth client config for the app (one row only)."""

    __tablename__ = "google_tasks_config"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, default=GOOGLE_TASKS_CONFIG_ID
    )
    # Google OAuth client id (not secret — safe to show in the UI).
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet-encrypted OAuth client secret.
    client_secret_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Master switch: when off, the connect flow and sync are disabled app-wide.
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def is_configured(self) -> bool:
        """Whether client credentials are present so the flow can run."""
        return bool(self.client_id and self.client_secret_encrypted)

    def __repr__(self) -> str:
        return f"<GoogleTasksConfig enabled={self.is_enabled} configured={self.is_configured}>"
