"""add project share links (public customer status pages)

A single brand-new table backing the read-only, no-login customer portal:
- project_share_links: one row per project, carrying a high-entropy public token,
  an enable/disable flag, creator, and lightweight view telemetry.

Brand-new table (no ALTER on existing tables), so there are no FTS triggers to
worry about.

Revision ID: 0028_add_project_share_links
Revises: 0027_add_poc_templates
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_add_project_share_links"
down_revision: str | Sequence[str] | None = "0027_add_poc_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_share_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=150), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("project_share_links", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_share_links_project_id"),
            ["project_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_project_share_links_token"),
            ["token"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_table("project_share_links")
