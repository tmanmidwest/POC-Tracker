"""add salesforce_opp_url to projects

Revision ID: 0002_sf_opp
Revises: 222f9872e4c8
Create Date: 2026-06-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_sf_opp"
down_revision: str | Sequence[str] | None = "222f9872e4c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("salesforce_opp_url", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "salesforce_opp_url")
