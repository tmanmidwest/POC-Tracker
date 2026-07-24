"""External OIDC identity provider configuration (e.g. Authentik).

Each row is one upstream provider the UI can authenticate against. Multiple
providers can be configured and enabled at once; the login page renders a
"Sign in with …" button per enabled provider. The client secret is encrypted
at rest (see app.services.secret_box) because it must be sent back to the
provider on every token exchange.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser

DEFAULT_SCOPES = "openid email profile"

# Tier assigned to users provisioned (JIT) through this provider. "standard" =
# internal user (an SE — full edit access, region-scoped when region enforcement
# is on); "external" = read-only viewer scoped to granted projects. The manager
# role isn't provisioned via SSO tier — an admin sets it in Settings → Users.
TIER_STANDARD = "standard"
TIER_EXTERNAL = "external"


class AuthProvider(Base, TimestampMixin):
    """A configured OIDC provider for UI single sign-on."""

    __tablename__ = "auth_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # URL-safe identifier used in the callback path (/ui/auth/<slug>/callback)
    # and as the IdP "app" name. Must match what's registered at the provider.
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # OIDC issuer URL; the discovery document is fetched from
    # <issuer>/.well-known/openid-configuration.
    issuer_url: Mapped[str] = mapped_column(String(500), nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Fernet-encrypted; may be empty for public (PKCE-only) clients.
    client_secret_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default=DEFAULT_SCOPES)
    # Tier for users provisioned through this provider: "standard" | "external".
    default_user_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TIER_STANDARD
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_by: Mapped[AppUser | None] = relationship("AppUser")

    def __repr__(self) -> str:
        return f"<AuthProvider slug={self.slug!r} display_name={self.display_name!r}>"
