"""add project type lookup and projects.type_id

Adds ``project_types`` — an admin-managed lookup of the kind of engagement a
POC is (Workshop, POC Playbook, POC Full Stack, …), mirroring ``feature_types``
(name + description, listed alphabetically, no sort_order). Also adds a nullable
``projects.type_id`` FK so the dashboard can group projects by type. Existing
projects keep a NULL type and appear in an "Untyped" bucket until edited.

Revision ID: 0031_add_project_type
Revises: 0030_add_project_category_orders
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_add_project_type"
down_revision: str | Sequence[str] | None = "0030_add_project_category_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_project_types_name"),
    )
    # Plain ADD COLUMN (native on SQLite) rather than batch_alter_table, so the
    # existing ``projects`` table is NOT recreated — recreating it would drop the
    # FTS search-index triggers (si_project_*) from migration 0012. SQLite can't
    # add a FK constraint to an existing table without that recreate, so the
    # column carries no DB-level FK; the ORM relationship and the lookup
    # delete-guard (references=… on projects.type_id) enforce integrity. Mirrors
    # how 0014/0015 add project columns.
    op.add_column(
        "projects", sa.Column("type_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_projects_type_id"), "projects", ["type_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_type_id"), table_name="projects")
    op.drop_column("projects", "type_id")
    op.drop_table("project_types")
