"""add sidebar_collapsed preference to app_users

Revision ID: 0032_sidebar_collapsed
Revises: 0031_add_project_type
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_sidebar_collapsed"
down_revision: str | Sequence[str] | None = "0031_add_project_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column(
            "sidebar_collapsed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("app_users", "sidebar_collapsed")
