"""add email/company to app_users and the user_invites table

Phase 2 of external-user invitations. Adds:
- app_users.email (unique) and app_users.company.
- user_invites: one row per invitation, with a hashed single-use token, expiry,
  status, and the project it was about.

Uses plain ADD COLUMN (not batch_alter_table) so no table is recreated; app_users
carries no FTS triggers, but this stays consistent with the 0023 note.

Revision ID: 0025_add_user_invites
Revises: 0024_add_smtp_config
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_add_user_invites"
down_revision: str | Sequence[str] | None = "0024_add_smtp_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("app_users", sa.Column("email", sa.String(length=320), nullable=True))
    op.add_column(
        "app_users", sa.Column("company", sa.String(length=200), nullable=True)
    )
    op.create_index(
        op.f("ix_app_users_email"), "app_users", ["email"], unique=True
    )

    op.create_table(
        "user_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("invited_name", sa.String(length=200), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"], ["app_users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user_invites", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_invites_user_id"), ["user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_invites_project_id"), ["project_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_user_invites_token_hash"), ["token_hash"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_user_invites_status"), ["status"], unique=False
        )


def downgrade() -> None:
    op.drop_table("user_invites")
    op.drop_index(op.f("ix_app_users_email"), table_name="app_users")
    op.drop_column("app_users", "company")
    op.drop_column("app_users", "email")
