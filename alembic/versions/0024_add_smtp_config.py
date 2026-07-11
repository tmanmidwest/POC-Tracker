"""add smtp_config singleton table

Outbound email settings for the app (used by external-user invitations). One row,
fixed primary key of 1. The SMTP password is stored Fernet-encrypted.

Revision ID: 0024_add_smtp_config
Revises: 0023_add_internal_only
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_add_smtp_config"
down_revision: str | Sequence[str] | None = "0023_add_internal_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "smtp_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column(
            "security", sa.String(length=20), nullable=False, server_default="starttls"
        ),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("password_encrypted", sa.String(length=500), nullable=True),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("from_name", sa.String(length=200), nullable=True),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("smtp_config")
