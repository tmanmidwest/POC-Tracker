"""add feedback tables

User feedback: an admin-managed feedback_statuses lookup and a feedback table of
bug reports / feature requests. Any signed-in user may submit; only admins manage
the queue (status, priority, internal notes). The submitter is a nullable FK
(ON DELETE SET NULL) plus a submitter_label snapshot so items survive account
removal.

New tables only, so batch_alter_table for index creation is safe here (it does
not touch the projects table or its FTS search-index triggers).

Revision ID: 0035_feedback
Revises: 0034_add_milestones
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_feedback"
down_revision: str | Sequence[str] | None = "0034_add_milestones"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Admin-managed status lookup ---
    op.create_table(
        "feedback_statuses",
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

    # --- Submitted feedback ---
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submitter_user_id", sa.Integer(), nullable=True),
        sa.Column("submitter_label", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["submitter_user_id"], ["app_users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["status_id"], ["feedback_statuses.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_feedback_submitter_user_id"), ["submitter_user_id"]
        )
        batch_op.create_index(batch_op.f("ix_feedback_kind"), ["kind"])
        batch_op.create_index(batch_op.f("ix_feedback_status_id"), ["status_id"])


def downgrade() -> None:
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_feedback_status_id"))
        batch_op.drop_index(batch_op.f("ix_feedback_kind"))
        batch_op.drop_index(batch_op.f("ix_feedback_submitter_user_id"))
    op.drop_table("feedback")
    op.drop_table("feedback_statuses")
