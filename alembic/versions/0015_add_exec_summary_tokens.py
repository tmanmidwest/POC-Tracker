"""add exec_summary_tokens to projects

Records how many tokens the AI used to produce the current executive summary, so
the cost/effort of a generation is visible (like the use-case importer already
shows).

Revision ID: 0015_exec_summary_tokens
Revises: 0014_ai_providers
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_exec_summary_tokens"
down_revision: str | Sequence[str] | None = "0014_ai_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("exec_summary_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "exec_summary_tokens")
