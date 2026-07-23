"""add regions lookup table

Adds ``regions`` — an admin-managed lookup of geographic regions (AMER, EMEA,
APAC, …) used for role-based access scoping. Mirrors the other lookup tables
(``project_statuses``: name + sort_order + is_active + is_system). Seeds a
single system "Unassigned" region (id chosen by the DB) that serves as the
fallback bucket for users/projects without an explicit region during rollout.

New table only, so batch_alter_table for index creation is safe here (it does
not touch the projects table or its FTS search-index triggers).

Revision ID: 0036_add_regions
Revises: 0035_feedback
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0036_add_regions"
down_revision: str | Sequence[str] | None = "0035_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    regions = op.create_table(
        "regions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_regions_name"),
    )

    # Seed the system "Unassigned" bucket. Kept first (sort_order 0) so it is the
    # obvious fallback in pickers; is_system=True guards it from deletion/rename.
    now = datetime.now(UTC)
    op.bulk_insert(
        regions,
        [
            {
                "name": "Unassigned",
                "sort_order": 0,
                "description": "Fallback region for users/projects "
                "not yet assigned to a real region.",
                "is_active": True,
                "is_system": True,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("regions")
