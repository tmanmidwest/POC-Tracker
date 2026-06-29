"""add named library sets (multiple use-case libraries)

Adds:
- ``library_sets`` — a named, grouping container for use-case library entries
  (e.g. "Standard", or a per-product / early-adoption library).
- ``use_case_library.library_set_id`` — every template entry now belongs to one
  library. Existing entries are migrated into a seeded "Standard" library.
- ``project_use_cases.library_set_id`` — provenance snapshot of which library a
  copied use case came from (nullable; a project can pull from many libraries).

Revision ID: 0017_library_sets
Revises: 0016_poc_instance_url
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_library_sets"
down_revision: str | Sequence[str] | None = "0016_poc_instance_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_sets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("library_sets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_library_sets_name"), ["name"], unique=True)

    # Seed the default library so existing entries have a home.
    op.execute(
        "INSERT INTO library_sets (name, description, is_active, created_at, updated_at) "
        "VALUES ('Standard', 'Default use case library.', 1, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )

    # use_case_library.library_set_id — add nullable, backfill, then enforce NOT NULL.
    op.add_column(
        "use_case_library",
        sa.Column("library_set_id", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE use_case_library SET library_set_id = "
        "(SELECT id FROM library_sets WHERE name = 'Standard')"
    )
    with op.batch_alter_table("use_case_library", schema=None) as batch_op:
        batch_op.alter_column(
            "library_set_id", existing_type=sa.Integer(), nullable=False
        )
        batch_op.create_index(
            batch_op.f("ix_use_case_library_library_set_id"),
            ["library_set_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_use_case_library_library_set_id_library_sets"),
            "library_sets",
            ["library_set_id"],
            ["id"],
        )

    # project_use_cases.library_set_id — provenance only (nullable, SET NULL).
    op.add_column(
        "project_use_cases",
        sa.Column("library_set_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("project_use_cases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_use_cases_library_set_id"),
            ["library_set_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_project_use_cases_library_set_id_library_sets"),
            "library_sets",
            ["library_set_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("project_use_cases", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_project_use_cases_library_set_id_library_sets"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_project_use_cases_library_set_id"))
        batch_op.drop_column("library_set_id")

    with op.batch_alter_table("use_case_library", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_use_case_library_library_set_id_library_sets"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_use_case_library_library_set_id"))
        batch_op.drop_column("library_set_id")

    with op.batch_alter_table("library_sets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_library_sets_name"))
    op.drop_table("library_sets")
