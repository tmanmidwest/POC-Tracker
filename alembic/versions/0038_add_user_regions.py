"""add user_regions membership table

Adds ``user_regions`` — the many-to-many link between app users and regions that
drives region-based access control. A standard SE gets one row (their home
region); a manager gets several. Admins and external viewers ignore it. Both FKs
cascade on delete. A unique (user_id, region_id) prevents duplicate memberships.

New table only (links app_users ↔ regions, not the projects table), so real
DB-level FKs and index creation are fine — the projects-table FTS caveat does
not apply.

Revision ID: 0038_add_user_regions
Revises: 0037_add_user_manager_role
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_add_user_regions"
down_revision: str | Sequence[str] | None = "0037_add_user_manager_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_regions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("region_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["region_id"], ["regions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "region_id", name="uq_user_region_user_region"
        ),
    )
    op.create_index(
        op.f("ix_user_regions_user_id"), "user_regions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_regions_region_id"), "user_regions", ["region_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_regions_region_id"), table_name="user_regions")
    op.drop_index(op.f("ix_user_regions_user_id"), table_name="user_regions")
    op.drop_table("user_regions")
