"""add notebook_url to projects

Revision ID: 0009_notebook_url
Revises: 0008_user_display
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_notebook_url"
down_revision: str | Sequence[str] | None = "0008_user_display"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("notebook_url", sa.String(length=1000), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "notebook_url")
