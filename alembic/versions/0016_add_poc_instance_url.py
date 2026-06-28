"""add poc_instance_url to projects

Revision ID: 0016_poc_instance_url
Revises: 0015_exec_summary_tokens
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_poc_instance_url"
down_revision: str | Sequence[str] | None = "0015_exec_summary_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("poc_instance_url", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "poc_instance_url")
