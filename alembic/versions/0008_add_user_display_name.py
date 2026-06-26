"""add display_name to app_users

Revision ID: 0008_user_display
Revises: 0007_user_theme
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_user_display"
down_revision: str | Sequence[str] | None = "0007_user_theme"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("display_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("app_users", "display_name")
