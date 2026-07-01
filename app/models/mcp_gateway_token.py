"""Inbound MCP gateway tokens.

Bearer tokens an external app or gateway (Saviynt, another project, etc.) presents
to authenticate **to** the MCP server's HTTP transports. Unlike the single legacy
gateway token, these are multiple, individually named and revocable records, so a
distinct token can be issued per consumer and invalidated on its own.

The MCP server runs as a separate container with no database access, so it can't
read these rows directly. Instead the app syncs the *hashes* of the active tokens
to a file on the shared data volume (see :mod:`app.services.mcp_gateway_tokens`),
and the MCP middleware verifies presented tokens against that file live.

As with API keys, the full token is shown only once at creation; only the SHA-256
hash and a short prefix (for identification) are persisted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser


class McpGatewayToken(Base, TimestampMixin):
    """A named, revocable bearer token for inbound MCP gateway access."""

    __tablename__ = "mcp_gateway_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id"), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[AppUser] = relationship("AppUser")

    def __repr__(self) -> str:
        return (
            f"<McpGatewayToken id={self.id} name={self.name!r} "
            f"prefix={self.token_prefix!r}>"
        )
