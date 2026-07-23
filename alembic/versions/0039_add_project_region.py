"""add projects.region_id

Adds a nullable ``projects.region_id`` — the region a POC belongs to, the axis
for region-based access control. Existing projects keep a NULL region until the
Phase 4 backfill derives it from the assigned SE's region (orphans → the system
"Unassigned" region).

Plain ADD COLUMN (native on SQLite) rather than batch_alter_table, so the
existing ``projects`` table is NOT recreated — recreating it would drop the FTS
search-index triggers (si_project_*) from migration 0012. SQLite can't add a FK
constraint to an existing table without that recreate, so the column carries no
DB-level FK; the ORM relationship (Project.region) enforces integrity. Mirrors
0031 (projects.type_id).

Revision ID: 0039_add_project_region
Revises: 0038_add_user_regions
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_add_project_region"
down_revision: str | Sequence[str] | None = "0038_add_user_regions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("region_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        op.f("ix_projects_region_id"), "projects", ["region_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_region_id"), table_name="projects")
    op.drop_column("projects", "region_id")
