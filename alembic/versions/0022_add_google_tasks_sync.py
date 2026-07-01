"""add google tasks sync tables

Phase-2 Google Tasks integration. Adds:
- google_tasks_config: singleton admin config holding the Google OAuth client
  id + Fernet-encrypted client secret and an enable switch.
- user_google_credentials: one row per user who connects their Google account,
  storing the encrypted refresh token, their dedicated task-list id, and status.

The tasks table already carries the reserved sync columns (sync_enabled,
external_id, external_etag, last_synced_at) from migration 0021, so no change to
tasks is needed here.

Revision ID: 0022_google_tasks_sync
Revises: 0021_task_manager
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_google_tasks_sync"
down_revision: str | Sequence[str] | None = "0021_task_manager"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "google_tasks_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("client_secret_encrypted", sa.String(length=500), nullable=True),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_google_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(length=500), nullable=True),
        sa.Column("google_email", sa.String(length=320), nullable=True),
        sa.Column("tasklist_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["app_user_id"], ["app_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user_google_credentials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_user_google_credentials_app_user_id"),
            ["app_user_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("user_google_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_google_credentials_app_user_id"))
    op.drop_table("user_google_credentials")
    op.drop_table("google_tasks_config")
