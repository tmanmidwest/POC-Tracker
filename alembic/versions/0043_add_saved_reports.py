"""add saved_reports: the user-defined reporting layer

Phase 1 of the reporting plan (docs/reporting-plan.md). One brand-new table for
saved/shared/published/schedulable reports. It links only to ``app_users`` (owner,
ON DELETE SET NULL so published/shared reports outlive their author), so the
``projects`` FTS-trigger (``batch_alter_table``) caveat does not apply.

The report definition and audience/schedule are stored as native ``JSONB`` (the
app is Postgres-only), so definitions stay queryable/indexable in SQL later
without a schema change. The definition is always validated against
``app.services.reporting.registry`` in the service layer before it is written.

The registry marks several project/task/use_case date columns as
``needs_index`` for filter/sort push-down *at scale*; v1 evaluates field filters
in Python over the region-scoped, capped result set, so those indexes are
deliberately deferred until data volume justifies them. Only the new table's own
indexes are created here.

Revision ID: 0043_add_saved_reports
Revises: 0042_add_rbac_dynamic_flag
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0043_add_saved_reports"
down_revision: str | Sequence[str] | None = "0042_add_rbac_dynamic_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("entity", sa.String(length=20), nullable=False),
        sa.Column(
            "is_summary", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "visibility",
            sa.String(length=12),
            nullable=False,
            server_default="private",
        ),
        sa.Column("audience", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["app_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_saved_reports_owner_id"), "saved_reports", ["owner_id"], unique=False
    )
    op.create_index(
        op.f("ix_saved_reports_entity"), "saved_reports", ["entity"], unique=False
    )
    op.create_index(
        op.f("ix_saved_reports_visibility"),
        "saved_reports",
        ["visibility"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_saved_reports_visibility"), table_name="saved_reports")
    op.drop_index(op.f("ix_saved_reports_entity"), table_name="saved_reports")
    op.drop_index(op.f("ix_saved_reports_owner_id"), table_name="saved_reports")
    op.drop_table("saved_reports")
