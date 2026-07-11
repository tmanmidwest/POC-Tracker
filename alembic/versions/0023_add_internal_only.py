"""add internal-only flag to project notes and tasks

Adds a boolean ``is_internal_only`` to both ``project_notes`` and ``tasks``.
When true, the record is hidden from external (viewer) users; internal users
always see it. Defaults to false (server_default 0) so all existing and new
records are fully visible unless explicitly marked internal-only.

Uses a plain ``ALTER TABLE ... ADD COLUMN`` (not ``batch_alter_table``) on
purpose: batch mode recreates the table on SQLite, which would silently drop
the FTS search-index triggers attached to ``project_notes`` in migration 0012.
An in-place ADD COLUMN leaves those triggers intact. The column isn't indexed —
nothing filters on it in SQL (visibility is filtered in Python), so an index
would only add write cost.

Revision ID: 0023_add_internal_only
Revises: 0022_google_tasks_sync
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_add_internal_only"
down_revision: str | Sequence[str] | None = "0022_google_tasks_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_notes",
        sa.Column(
            "is_internal_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "is_internal_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "is_internal_only")
    op.drop_column("project_notes", "is_internal_only")
