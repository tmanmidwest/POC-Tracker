"""add POC templates

Phase 2 of the New POC wizard. Adds reusable POC blueprints:
- poc_templates: a named template with an optional default project status.
- poc_template_use_cases: snapshotted use cases belonging to a template.
- poc_template_tasks: kickoff-task blueprints with day offsets (resolved to real
  dates when the template is applied).

All three are brand-new tables (no ALTER on existing tables), so there are no FTS
triggers to worry about.

Revision ID: 0027_add_poc_templates
Revises: 0026_external_user_expiry
Create Date: 2026-07-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_add_poc_templates"
down_revision: str | Sequence[str] | None = "0026_external_user_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "poc_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_status_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=150), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["default_status_id"], ["project_statuses.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("poc_templates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_poc_templates_name"), ["name"], unique=True
        )

    op.create_table(
        "poc_template_use_cases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="custom"),
        sa.Column("library_id", sa.Integer(), nullable=True),
        sa.Column("reference_number", sa.String(length=20), nullable=True),
        sa.Column("category", sa.String(length=150), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("success_validation", sa.Text(), nullable=True),
        sa.Column("feature_type_id", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["poc_templates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["use_case_library.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["feature_type_id"], ["feature_types.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("poc_template_use_cases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_poc_template_use_cases_template_id"),
            ["template_id"],
            unique=False,
        )

    op.create_table(
        "poc_template_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("start_offset_days", sa.Integer(), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["poc_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("poc_template_tasks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_poc_template_tasks_template_id"),
            ["template_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("poc_template_tasks")
    op.drop_table("poc_template_use_cases")
    op.drop_table("poc_templates")
