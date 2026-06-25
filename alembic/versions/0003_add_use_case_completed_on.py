"""add completed_on to project_use_cases

Revision ID: 0003_uc_completed_on
Revises: 0002_sf_opp
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_uc_completed_on"
down_revision: str | Sequence[str] | None = "0002_sf_opp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_use_cases",
        sa.Column("completed_on", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_use_cases", "completed_on")
