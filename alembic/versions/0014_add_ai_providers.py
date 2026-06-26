"""add AI providers and project executive-summary columns

Adds:
- ``ai_providers`` — configured text-generation providers (Anthropic, etc.),
  with an encrypted API key, model id, enabled flag, and a single default.
- ``projects.exec_summary*`` — the AI-generated executive summary, its editable
  HTML, when it was generated, and which provider/model produced it.

Revision ID: 0014_ai_providers
Revises: 0013_project_grants
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_ai_providers"
down_revision: str | Sequence[str] | None = "0013_project_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column(
            "api_key_encrypted", sa.String(length=1000), nullable=False, server_default=""
        ),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("app_users.id"),
            nullable=True,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("projects", sa.Column("exec_summary", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("exec_summary_html", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("exec_summary_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects", sa.Column("exec_summary_model", sa.String(length=150), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("projects", "exec_summary_model")
    op.drop_column("projects", "exec_summary_generated_at")
    op.drop_column("projects", "exec_summary_html")
    op.drop_column("projects", "exec_summary")
    op.drop_table("ai_providers")
