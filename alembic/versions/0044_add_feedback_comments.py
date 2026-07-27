"""add feedback_comments and retire feedback.admin_notes

Replaces the single overwritable ``feedback.admin_notes`` blob with a proper
comment timeline (``feedback_comments``): admin-authored, one row per comment,
so tracking/updates/closure form a durable history instead of one field.

Any existing ``admin_notes`` text is preserved by copying it into a first
comment on each item (authored by nobody — a ``Legacy internal note`` label),
after which the column is dropped.

New table + a column drop; neither touches the projects table or its FTS search
triggers, so batch_alter_table for index creation stays safe.

Revision ID: 0044_add_feedback_comments
Revises: 0043_add_saved_reports
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_add_feedback_comments"
down_revision: str | Sequence[str] | None = "0043_add_saved_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_label", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["feedback_id"], ["feedback.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["author_user_id"], ["app_users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("feedback_comments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_feedback_comments_feedback_id"), ["feedback_id"]
        )
        batch_op.create_index(
            batch_op.f("ix_feedback_comments_author_user_id"), ["author_user_id"]
        )

    # Preserve any existing internal notes as the first comment on each item.
    op.execute(
        """
        INSERT INTO feedback_comments
            (feedback_id, author_user_id, author_label, body, created_at, updated_at)
        SELECT id, NULL, 'Legacy internal note', admin_notes, created_at, created_at
        FROM feedback
        WHERE admin_notes IS NOT NULL AND btrim(admin_notes) <> ''
        """
    )

    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.drop_column("admin_notes")


def downgrade() -> None:
    with op.batch_alter_table("feedback", schema=None) as batch_op:
        batch_op.add_column(sa.Column("admin_notes", sa.Text(), nullable=True))

    with op.batch_alter_table("feedback_comments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_feedback_comments_author_user_id"))
        batch_op.drop_index(batch_op.f("ix_feedback_comments_feedback_id"))
    op.drop_table("feedback_comments")
