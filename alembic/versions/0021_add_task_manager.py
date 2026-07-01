"""add task manager tables

Per-user Task Manager: admin-managed task_statuses and task_priorities (global
lookups), user-owned tasks (optionally assigned to a project), and per-user
task_dashboard_prefs. Also adds app_config.tasks_enabled to toggle the module.

The tasks table carries reserved sync_* columns for the phase-2 Google Tasks
integration so enabling sync later needs no schema change.

Revision ID: 0021_task_manager
Revises: 0020_mcp_gateway_tokens
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_task_manager"
down_revision: str | Sequence[str] | None = "0020_mcp_gateway_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Global lookups (admin-managed) ---
    op.create_table(
        "task_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_terminal", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "task_priorities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # --- User-owned tasks ---
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status_id", sa.Integer(), nullable=False),
        sa.Column("priority_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("details_html", sa.Text(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_etag", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["app_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["status_id"], ["task_statuses.id"]),
        sa.ForeignKeyConstraint(["priority_id"], ["task_priorities.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tasks_owner_user_id"), ["owner_user_id"])
        batch_op.create_index(batch_op.f("ix_tasks_status_id"), ["status_id"])
        batch_op.create_index(batch_op.f("ix_tasks_priority_id"), ["priority_id"])
        batch_op.create_index(batch_op.f("ix_tasks_project_id"), ["project_id"])
        batch_op.create_index(batch_op.f("ix_tasks_is_archived"), ["is_archived"])

    # --- Per-user task dashboard preferences ---
    op.create_table(
        "task_dashboard_prefs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["app_user_id"], ["app_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("task_dashboard_prefs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_task_dashboard_prefs_app_user_id"),
            ["app_user_id"],
            unique=True,
        )

    # --- Module toggle on the singleton app_config row ---
    with op.batch_alter_table("app_config", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tasks_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_config", schema=None) as batch_op:
        batch_op.drop_column("tasks_enabled")

    with op.batch_alter_table("task_dashboard_prefs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_dashboard_prefs_app_user_id"))
    op.drop_table("task_dashboard_prefs")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_is_archived"))
        batch_op.drop_index(batch_op.f("ix_tasks_project_id"))
        batch_op.drop_index(batch_op.f("ix_tasks_priority_id"))
        batch_op.drop_index(batch_op.f("ix_tasks_status_id"))
        batch_op.drop_index(batch_op.f("ix_tasks_owner_user_id"))
    op.drop_table("tasks")

    op.drop_table("task_priorities")
    op.drop_table("task_statuses")
