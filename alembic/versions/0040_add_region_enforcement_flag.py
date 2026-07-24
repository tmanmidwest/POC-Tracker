"""add app_config.region_enforcement_enabled

Master switch for region-based access control. Defaults to False so existing
deployments keep legacy behavior (internal users see every project) until an
admin flips it on — after regions are defined, users are assigned, and projects
are backfilled. Read by access.py / scope.py to gate hard region boundaries.

Plain ADD COLUMN with a server_default so the existing singleton config row
backfills to False. ``app_config`` has no FTS triggers, so this is safe.

Revision ID: 0040_add_region_enforcement_flag
Revises: 0039_add_project_region
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_add_region_enforcement_flag"
down_revision: str | Sequence[str] | None = "0039_add_project_region"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column(
            "region_enforcement_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_config", "region_enforcement_enabled")
