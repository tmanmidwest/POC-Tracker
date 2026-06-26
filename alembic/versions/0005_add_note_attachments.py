"""add note_attachments table

Revision ID: 0005_note_attach
Revises: 0004_project_notes
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_note_attach"
down_revision: str | Sequence[str] | None = "0004_project_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "note_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_note_id", sa.Integer(), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_note_id"], ["project_notes.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stored_filename"),
    )
    op.create_index(
        "ix_note_attachments_project_note_id", "note_attachments", ["project_note_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_note_attachments_project_note_id", table_name="note_attachments")
    op.drop_table("note_attachments")
