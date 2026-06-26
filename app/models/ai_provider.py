"""Configured AI text-generation provider (e.g. Anthropic Claude, Google Gemini).

Each row is one provider the app can call to generate text (currently used for
executive summaries). The API key is encrypted at rest with Fernet — the same
treatment as an OIDC client secret — because it must be sent to the vendor on
every request. One enabled provider is marked ``is_default`` and is the one used
for generation; the rest are inactive alternatives an admin can switch to.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser


class AIProvider(Base, TimestampMixin):
    """A configured text-generation provider for AI features."""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Registry key that selects the implementation: "anthropic" | "google" | ...
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Fernet-encrypted vendor API key (recoverable — sent on every request).
    api_key_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    # Vendor model id, e.g. "claude-opus-4-8".
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # The one enabled provider used for generation. At most one is default.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[AppUser | None] = relationship("AppUser")

    @property
    def has_key(self) -> bool:
        return bool(self.api_key_encrypted)

    def __repr__(self) -> str:
        return f"<AIProvider provider={self.provider!r} model={self.model!r}>"
