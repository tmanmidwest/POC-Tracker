"""Single-row system configuration (one row, fixed primary key of 1).

Holds operational settings an admin can change from the UI without redeploying —
currently just the audit/activity log retention window. See
app.services.system_config for the read-side cache and accessors.

The initial value, when the row is first created, comes from the
POCT_AUDIT_RETENTION_DAYS env var (settings.audit_retention_days), so a build
can still set a starting default; the UI value then persists and overrides it.
"""

from __future__ import annotations

from sqlalchemy import Integer
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

    def __repr__(self) -> str:
        return f"<AppConfig audit_retention_days={self.audit_retention_days}>"
