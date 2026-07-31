"""swap app_config.brandfetch_client_id for logodev_token

Brandfetch's Logo API forbids programmatic/server-side fetching (it 403s
datacenter requests — it's built for browser <img> embedding only), which is
incompatible with our fetch-and-store model (logos are downloaded, normalized to
a PNG on disk, and baked into the portal and PPTX/PDF reports). Logo.dev permits
server-side downloading, so the branded-logo provider moves to Logo.dev.

Drops the now-useless Brandfetch client ID column and adds a Logo.dev publishable
token column. The old value is a Brandfetch credential and is intentionally not
migrated. ``app_config`` has no FTS triggers, so this is safe.

Revision ID: 0049_swap_brandfetch_for_logodev
Revises: 0048_add_brandfetch_client_id
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049_swap_brandfetch_for_logodev"
down_revision: str | Sequence[str] | None = "0048_add_brandfetch_client_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("logodev_token", sa.String(length=255), nullable=True),
    )
    op.drop_column("app_config", "brandfetch_client_id")


def downgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("brandfetch_client_id", sa.String(length=255), nullable=True),
    )
    op.drop_column("app_config", "logodev_token")
