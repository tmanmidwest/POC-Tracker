"""add use_case_view_prefs table

Revision ID: 0006_uc_view_prefs
Revises: 0005_note_attach
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_uc_view_prefs"
down_revision: str | Sequence[str] | None = "0005_note_attach"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "use_case_view_prefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id"),
    )
    op.create_index(
        "ix_use_case_view_prefs_app_user_id", "use_case_view_prefs", ["app_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_use_case_view_prefs_app_user_id", table_name="use_case_view_prefs")
    op.drop_table("use_case_view_prefs")
