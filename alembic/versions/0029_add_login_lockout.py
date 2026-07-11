"""add failed-login lockout and password-reset tokens

Adds brute-force protection for local (password) accounts. Two columns on
``app_users`` track failed sign-ins: ``failed_login_count`` (running tally,
reset on success) and ``locked_at`` (non-null once the account is locked after
too many failures). Lockout is strict — a locked account stays locked until an
admin unlocks it or the user completes a password reset.

The ``password_reset_tokens`` table mirrors ``user_invites``: a single-use,
expiring token stored only as a SHA-256 hash (the plaintext is emailed once).

Plain ADD COLUMN (not batch_alter_table) on app_users so no table is recreated
and no FTS triggers on related tables are dropped.

Revision ID: 0029_add_login_lockout
Revises: 0028_add_project_share_links
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_add_login_lockout"
down_revision: str | Sequence[str] | None = "0028_add_project_share_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "app_users",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("password_reset_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_user_id"), ["user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_token_hash"),
            ["token_hash"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_password_reset_tokens_status"), ["status"], unique=False
        )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_column("app_users", "locked_at")
    op.drop_column("app_users", "failed_login_count")
