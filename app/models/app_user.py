"""App user (admin account) model."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# The four mutually-exclusive user roles, resolved by ``AppUser.role``. Stored
# underneath as independent booleans; ``role`` is the canonical accessor.
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_STANDARD = "standard"
ROLE_EXTERNAL = "external"
# Order matters: ``role`` getter resolves the first matching flag, so more
# privileged / more specific roles come first.
VALID_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_STANDARD, ROLE_EXTERNAL)


class AppUser(Base, TimestampMixin):
    """Account that can log in to the web UI.

    The `is_seeded` flag identifies the bootstrapped admin account so the reset
    script can target it without affecting other accounts.

    `password_hash` is nullable: users provisioned via OIDC single sign-on have
    no local password and authenticate through their identity provider.

    `is_admin` puts the user in the Admin group (can do anything). Users without
    it are standard users: they can add/edit POC projects and use cases, but not
    the admin-only surfaces (lookups, users, library, auth providers, settings).

    Roles are stored as independent booleans (`is_admin`, `is_external`,
    `is_manager`) for backwards compatibility with existing queries and write
    sites. The `role` property is the single read/write accessor that resolves
    them into one of ``admin | manager | standard | external`` (see ``role``).
    A **manager** is an internal, non-admin user who — unlike a standard SE —
    can view and edit POCs across every region assigned to them (memberships live
    in ``user_regions`` / the ``UserRegion`` model; resolved by
    ``app.services.access.allowed_region_ids``). ``is_manager`` alone confers no
    extra rights without region assignments.
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
    # Manager: an internal, non-admin user who can view+edit POCs across all the
    # regions assigned to them (a standard SE is hard-scoped to their own region).
    # Only meaningful when not admin and not external. Set via the ``role`` setter.
    is_manager: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # UI color theme preference: "light" | "dark" (None = light default).
    theme: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Collapse the desktop sidebar to an icon-only rail (per-user, persists).
    sidebar_collapsed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Friendly name shown in the UI (e.g. "Robby Smith"); falls back to username.
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Email address. Null for legacy/internal accounts created before this field
    # and for OIDC users (their email lives on UserIdentity). Set for invited
    # external users, where it's also their username / login id. Unique so it can
    # identify an account; SQLite treats multiple NULLs as distinct.
    email: Mapped[str | None] = mapped_column(
        String(320), unique=True, index=True, nullable=True
    )
    # The external user's company/organization (shown in the External users list).
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # When this (external) account auto-expires — the daily sweep deactivates it
    # once past. Null means "never expires" (internal users, or externals whose
    # term is disabled). Set at invite acceptance to acceptance + the configured
    # term; extended from the UI. See app.services.external_expiry.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the pre-expiry warning was last emailed to the project SE(s). Stamped
    # so the warning goes out once per term; cleared when the account is extended.
    expiry_warning_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Consecutive failed local sign-ins. Reset to 0 on any successful login or
    # when an admin/reset unlocks the account. Only counted for password users.
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # When the account was locked after too many failed sign-ins. Non-null means
    # locked: strict lockout, cleared only by an admin unlock or a password reset
    # (there is no time-based auto-unlock). See app.services.login_security.
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_locked(self) -> bool:
        """True if the account is locked out of local password sign-in."""
        return self.locked_at is not None

    @property
    def display_label(self) -> str:
        """The name to show in the UI — display_name if set, else the username."""
        return self.display_name or self.username

    @property
    def is_internal(self) -> bool:
        """Internal users (admin, manager, or standard) can edit; not read-only."""
        return not self.is_external

    @property
    def role(self) -> str:
        """Canonical single role, resolved from the underlying boolean flags.

        Returns one of ``admin | manager | standard | external`` (see
        ``VALID_ROLES``). Admin wins over external wins over manager, so any
        redundant flag combination still maps to exactly one sensible role.
        """
        if self.is_admin:
            return ROLE_ADMIN
        if self.is_external:
            return ROLE_EXTERNAL
        if self.is_manager:
            return ROLE_MANAGER
        return ROLE_STANDARD

    @role.setter
    def role(self, value: str) -> None:
        """Set the role by name, mapping deterministically to the boolean flags.

        Unambiguous: exactly one flag (at most) is set, the rest cleared. Raises
        ValueError on an unknown role so a bad form value can't silently no-op.
        """
        if value not in VALID_ROLES:
            raise ValueError(f"Unknown role {value!r}; expected one of {VALID_ROLES}")
        self.is_admin = value == ROLE_ADMIN
        self.is_external = value == ROLE_EXTERNAL
        self.is_manager = value == ROLE_MANAGER

    @property
    def is_manager_role(self) -> bool:
        """True only when the resolved role is exactly ``manager``.

        Distinct from the raw ``is_manager`` flag, which an admin/external user
        could also carry redundantly; this respects the ``role`` precedence.
        """
        return self.role == ROLE_MANAGER

    @property
    def expires_at_aware(self) -> datetime | None:
        """``expires_at`` coerced to timezone-aware UTC (SQLite drops tzinfo)."""
        exp = self.expires_at
        if exp is not None and exp.tzinfo is None:
            return exp.replace(tzinfo=UTC)
        return exp

    @property
    def is_expired(self) -> bool:
        """True once past the expiry moment (independent of the sweep running)."""
        exp = self.expires_at_aware
        return exp is not None and exp < datetime.now(UTC)

    @property
    def days_until_expiry(self) -> int | None:
        """Whole days until expiry (negative if already past); None if no expiry."""
        exp = self.expires_at_aware
        if exp is None:
            return None
        delta = exp - datetime.now(UTC)
        # Round toward zero on the day boundary so "today" reads as 0.
        return int(delta.total_seconds() // 86400)

    def __repr__(self) -> str:
        return f"<AppUser username={self.username!r}>"
