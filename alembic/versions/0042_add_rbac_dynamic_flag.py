"""add app_config.rbac_dynamic_enabled

Master switch for the dynamic-RBAC role builder. Defaults to False so existing
deployments keep legacy behavior (``user.can()`` resolves via the pre-role-builder
gates) until an admin flips it on — after roles are seeded, users are assigned,
and call sites are cut over. Read by ``AppUser.can()`` to decide whether to honor
role-based capabilities or fall back to the legacy tiers.

Plain ADD COLUMN with a server_default so the existing singleton config row
backfills to False. ``app_config`` has no FTS triggers, so this is safe.

Revision ID: 0042_add_rbac_dynamic_flag
Revises: 0041_add_rbac_roles
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_add_rbac_dynamic_flag"
down_revision: str | Sequence[str] | None = "0041_add_rbac_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column(
            "rbac_dynamic_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_config", "rbac_dynamic_enabled")
