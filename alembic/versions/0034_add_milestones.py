"""add POC milestones

Adds a shared, project-owned lifecycle timeline:
- project_milestones: dated checkpoints on a live POC (Kickoff, Readout, …).
- milestone_defaults: the global admin-managed standard lifecycle (name +
  day-offset) that new POCs seed from.
- poc_template_milestones: milestone blueprints inside a POC template, offset-
  based and re-anchored when applied (mirrors poc_template_tasks).

All three are brand-new tables (no ALTER on existing tables), so there are no FTS
triggers to worry about and batch_alter_table is safe.

Revision ID: 0034_add_milestones
Revises: 0033_add_win_loss_outcome
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_add_milestones"
down_revision: str | Sequence[str] | None = "0033_add_win_loss_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_milestones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("completed_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("project_milestones", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_project_milestones_project_id"),
            ["project_id"],
            unique=False,
        )

    op.create_table(
        "milestone_defaults",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("target_offset_days", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_milestone_defaults_name"),
    )

    op.create_table(
        "poc_template_milestones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("target_offset_days", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["poc_templates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("poc_template_milestones", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_poc_template_milestones_template_id"),
            ["template_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("poc_template_milestones")
    op.drop_table("milestone_defaults")
    op.drop_table("project_milestones")
