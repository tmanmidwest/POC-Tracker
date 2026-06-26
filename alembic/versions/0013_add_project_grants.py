"""add project grants, external-viewer tier, per-provider default tier

Adds the per-project access model:
- ``project_grants`` — one row per (project, user) grant (read access).
- ``app_users.is_external`` — marks read-only external viewers.
- ``auth_providers.default_user_tier`` — tier assigned to JIT-provisioned users.

Revision ID: 0013_project_grants
Revises: 0012_search_index
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_project_grants"
down_revision: str | Sequence[str] | None = "0012_search_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column(
            "is_external", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "auth_providers",
        sa.Column(
            "default_user_tier",
            sa.String(length=20),
            nullable=False,
            server_default="standard",
        ),
    )
    op.create_table(
        "project_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("app_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", sa.String(length=50), nullable=False, server_default="viewer"),
        sa.Column(
            "granted_by_user_id",
            sa.Integer(),
            sa.ForeignKey("app_users.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_grant_project_user"
        ),
    )
    op.create_index(
        "ix_project_grants_project_id", "project_grants", ["project_id"]
    )
    op.create_index("ix_project_grants_user_id", "project_grants", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_project_grants_user_id", table_name="project_grants")
    op.drop_index("ix_project_grants_project_id", table_name="project_grants")
    op.drop_table("project_grants")
    op.drop_column("auth_providers", "default_user_tier")
    op.drop_column("app_users", "is_external")
