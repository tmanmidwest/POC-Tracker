"""Single-row SMTP (outbound email) config (one row, fixed primary key of 1).

Holds the admin-provided settings the app uses to send outbound email — today,
external-user invitations. The SMTP password is encrypted at rest (Fernet, via
app.services.secret_box): it must be recoverable because we hand it to the SMTP
server on every send, so it can't be a one-way hash.

Mirrors the GoogleTasksConfig singleton pattern.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# The singleton row always uses this primary key.
SMTP_CONFIG_ID = 1

# Connection security modes.
SECURITY_NONE = "none"
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
VALID_SECURITY = (SECURITY_NONE, SECURITY_STARTTLS, SECURITY_SSL)


class SmtpConfig(Base, TimestampMixin):
    """Outbound SMTP server config for the app (one row only)."""

    __tablename__ = "smtp_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SMTP_CONFIG_ID)
    # Server host and port. 587 is the STARTTLS submission default.
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    # Connection security: "none" | "starttls" | "ssl".
    security: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SECURITY_STARTTLS
    )
    # Auth credentials. Optional — some relays accept unauthenticated submission
    # from trusted hosts. Password is Fernet-encrypted at rest.
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Envelope/header sender identity.
    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Master switch: when off, the app sends no mail.
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def is_configured(self) -> bool:
        """Whether the minimum needed to send is present (host + from address)."""
        return bool(self.host and self.from_email)

    @property
    def has_password(self) -> bool:
        return bool(self.password_encrypted)

    def __repr__(self) -> str:
        return f"<SmtpConfig enabled={self.is_enabled} configured={self.is_configured}>"
