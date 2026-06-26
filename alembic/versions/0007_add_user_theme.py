"""add theme preference to app_users

Revision ID: 0007_user_theme
Revises: 0006_uc_view_prefs
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_user_theme"
down_revision: str | Sequence[str] | None = "0006_uc_view_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("theme", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("app_users", "theme")
