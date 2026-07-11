"""add per-project use-case category ordering

Adds ``project_category_orders`` — an optional explicit sort position for a
use-case category within one project. Use-case categories are free text (no
lookup table), so by default the project page lists category sections
alphabetically. A row here pins a ``sort_order`` number to a category name;
numbered categories sort first (ascending), un-numbered ones fall back to
alphabetical. One row per (project, category).

New table only, so no existing table is recreated and no FTS triggers are
dropped.

Revision ID: 0030_add_project_category_orders
Revises: 0029_add_login_lockout
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_add_project_category_orders"
down_revision: str | Sequence[str] | None = "0029_add_login_lockout"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_category_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=150), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "category", name="uq_project_category_order"
        ),
    )
    with op.batch_alter_table("project_category_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_category_orders_project_id"),
            ["project_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("project_category_orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_category_orders_project_id"))
    op.drop_table("project_category_orders")
