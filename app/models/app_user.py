"""App user (admin account) model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AppUser(Base, TimestampMixin):
    """Account that can log in to the web UI.

    The `is_seeded` flag identifies the bootstrapped admin account so the reset
    script can target it without affecting other accounts.

    `password_hash` is nullable: users provisioned via OIDC single sign-on have
    no local password and authenticate through their identity provider.

    `is_admin` puts the user in the Admin group (can do anything). Users without
    it are standard users: they can add/edit POC projects and use cases, but not
    the admin-only surfaces (lookups, users, library, auth providers, settings).
    """

    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_seeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # External viewer: read-only, and only sees projects explicitly shared with
    # them (see ProjectGrant). Internal users (admin or standard) ignore grants.
    is_external: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # UI color theme preference: "light" | "dark" (None = light default).
    theme: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Friendly name shown in the UI (e.g. "Robby Smith"); falls back to username.
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    @property
    def display_label(self) -> str:
        """The name to show in the UI — display_name if set, else the username."""
        return self.display_name or self.username

    @property
    def is_internal(self) -> bool:
        """Internal users (admin or standard) see all projects and can edit."""
        return not self.is_external

    def __repr__(self) -> str:
        return f"<AppUser username={self.username!r}>"
