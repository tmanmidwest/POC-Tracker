"""add external-user account expiry

Adds a time-based expiration to external viewer accounts: ``expires_at`` (when the
account auto-deactivates) and ``expiry_warning_sent_at`` (so the pre-expiry SE
warning is emailed once per term). Also adds the configurable default term to
app_config (``external_user_ttl_days``, 60 = the shipped default, 0 = never).

Existing active external users are backfilled to a grace window of 60 days from
this migration's run so nobody is surprise-expired on first sweep.

Revision ID: 0026_external_user_expiry
Revises: 0025_add_user_invites
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alembic import op

revision: str = "0026_external_user_expiry"
down_revision: str | Sequence[str] | None = "0025_add_user_invites"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRACE_DAYS = 60


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "app_users",
        sa.Column("expiry_warning_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "app_config",
        sa.Column(
            "external_user_ttl_days",
            sa.Integer(),
            nullable=False,
            server_default="60",
        ),
    )

    # Give current active, accepted external users a grace window rather than
    # expiring long-lived accounts on the first sweep.
    grace = datetime.now(UTC) + timedelta(days=_GRACE_DAYS)
    op.get_bind().execute(
        sa.text(
            "UPDATE app_users SET expires_at = :exp "
            "WHERE is_external = 1 AND is_active = 1 "
            "AND password_hash IS NOT NULL AND expires_at IS NULL"
        ),
        {"exp": grace},
    )


def downgrade() -> None:
    op.drop_column("app_config", "external_user_ttl_days")
    op.drop_column("app_users", "expiry_warning_sent_at")
    op.drop_column("app_users", "expires_at")
