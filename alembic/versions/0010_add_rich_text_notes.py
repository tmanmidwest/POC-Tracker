"""add rich-text HTML columns for project notes

Adds ``body_html`` to ``project_notes`` and ``notes_html`` to ``projects`` so
notes can store sanitized rich-text HTML alongside the existing plain text.

Revision ID: 0010_rich_text_notes
Revises: 0009_notebook_url
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_rich_text_notes"
down_revision: str | Sequence[str] | None = "0009_notebook_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_notes", sa.Column("body_html", sa.Text(), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("notes_html", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "notes_html")
    op.drop_column("project_notes", "body_html")
