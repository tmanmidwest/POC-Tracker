"""add app_users.is_manager for the manager role

Adds a nullable-safe ``is_manager`` boolean to ``app_users`` for the new
**manager** role — an internal, non-admin user who can view+edit POCs across the
regions assigned to them. Stored as an independent flag alongside ``is_admin`` /
``is_external`` (the ``AppUser.role`` property resolves the three into one of
admin | manager | standard | external). Existing rows backfill to False via the
server_default, so every current user keeps their present role.

``app_users`` has no FTS search-index triggers, so a plain ADD COLUMN is fine
(the projects-table batch_alter_table caveat does not apply here).

Revision ID: 0037_add_user_manager_role
Revises: 0036_add_regions
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_add_user_manager_role"
down_revision: str | Sequence[str] | None = "0036_add_regions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column(
            "is_manager",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_users", "is_manager")
