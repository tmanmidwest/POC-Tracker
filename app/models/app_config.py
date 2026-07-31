"""Single-row system configuration (one row, fixed primary key of 1).

Holds operational settings an admin can change from the UI without redeploying —
currently just the audit/activity log retention window. See
app.services.system_config for the read-side cache and accessors.

The initial value, when the row is first created, comes from the
POCT_AUDIT_RETENTION_DAYS env var (settings.audit_retention_days), so a build
can still set a starting default; the UI value then persists and overrides it.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

# The singleton row always uses this primary key.
APP_CONFIG_ID = 1


class AppConfig(Base, TimestampMixin):
    """System settings for the app (one row only)."""

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=APP_CONFIG_ID)
    # Delete audit/activity events older than this many days. 0 = keep forever.
    audit_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    # Whether the per-user Task Manager module is enabled. When off, the Tasks
    # nav item and routes are hidden. Defaults on.
    tasks_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Default lifetime (days) granted to an external viewer account at invite
    # acceptance. The daily sweep auto-deactivates the account once past expiry.
    # 0 = external accounts never expire.
    external_user_ttl_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    # Master switch for region-based access control. When OFF (default), region
    # data is stored but NOT enforced — internal users still see every project
    # (legacy behavior). When ON, SEs are hard-scoped to their region(s)
    # and managers to their assigned regions. Kept off until regions/backfill are
    # verified so enabling it can't blank out the app. Read in access.py/scope.py.
    region_enforcement_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Master switch for the dynamic-RBAC role builder. When OFF (default),
    # ``AppUser.can()`` resolves capabilities via the legacy gates (admin /
    # internal / open) so behavior is identical to the four hardcoded roles. When
    # ON, ``can()`` reads the union of capabilities across the user's assigned
    # roles. Seeded roles reproduce legacy behavior, so flipping this is a no-op
    # for existing users. Kept off until call sites are cut over. See
    # app.services.rbac and docs/rbac-role-builder-plan.md.
    rbac_dynamic_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # Brandfetch Logo API client ID for "Fetch logo from website". A publishable
    # value (rides in public image URLs), so stored plain, not encrypted. NULL/empty
    # falls back to POCT_BRANDFETCH_CLIENT_ID, then to the keyless favicon source.
    brandfetch_client_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    def __repr__(self) -> str:
        return f"<AppConfig audit_retention_days={self.audit_retention_days}>"
