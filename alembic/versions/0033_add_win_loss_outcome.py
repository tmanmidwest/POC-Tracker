"""add win/loss outcome tracking

Makes POC outcomes machine-readable for win-rate analytics. Today "won" and
"lost" are only encoded in status *names* ("Completed - Won" / "Completed -
Lost"), which can't be aggregated cleanly and carry no reason or competitor.

This migration:
  - adds ``project_statuses.outcome`` (none | won | lost | no_decision) — the
    single source of truth for win/loss, derived from the status;
  - creates ``close_reasons``, an admin-managed lookup (mirrors ``contact_roles``);
  - adds ``projects.close_reason_id``, ``projects.competitor`` and
    ``projects.closed_date`` for the surrounding close context and cycle-time;
  - backfills: maps the seeded Won/Lost status names to outcomes, and stamps a
    best-effort ``closed_date`` (the last-updated date) on projects already in a
    terminal status so historical cycle-time isn't all null.

Plain ``op.add_column`` on ``projects`` (native on SQLite) — NOT
``batch_alter_table`` — so the table is not recreated and the FTS search-index
triggers (si_project_*) from 0012 survive. The new FK column therefore carries
no DB-level FK; the ORM relationship and the lookup delete-guard enforce it,
matching 0031.

Revision ID: 0033_add_win_loss_outcome
Revises: 0032_add_sidebar_collapsed
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_add_win_loss_outcome"
down_revision: str | Sequence[str] | None = "0032_sidebar_collapsed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Structured outcome on the status lookup (single source of truth).
    op.add_column(
        "project_statuses",
        sa.Column(
            "outcome",
            sa.String(length=20),
            nullable=False,
            server_default="none",
        ),
    )

    # 2. Close-reason lookup.
    op.create_table(
        "close_reasons",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_close_reasons_name"),
    )

    # 3. Close context on the project (no DB-level FK on SQLite; see docstring).
    op.add_column(
        "projects", sa.Column("close_reason_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("competitor", sa.String(length=200), nullable=True)
    )
    op.add_column("projects", sa.Column("closed_date", sa.Date(), nullable=True))
    op.create_index(
        op.f("ix_projects_close_reason_id"),
        "projects",
        ["close_reason_id"],
        unique=False,
    )

    # 4. Backfill outcomes from the seeded terminal status names.
    project_statuses = sa.table(
        "project_statuses",
        sa.column("name", sa.String),
        sa.column("outcome", sa.String),
    )
    op.execute(
        project_statuses.update()
        .where(project_statuses.c.name == "Completed - Won")
        .values(outcome="won")
    )
    op.execute(
        project_statuses.update()
        .where(project_statuses.c.name == "Completed - Lost")
        .values(outcome="lost")
    )

    # 5. Best-effort closed_date for projects already in a terminal status, so
    #    historical cycle-time has data. Uses the last-updated date as a proxy.
    op.execute(
        """
        UPDATE projects
        SET closed_date = date(updated_at)
        WHERE closed_date IS NULL
          AND status_id IN (
            SELECT id FROM project_statuses WHERE is_terminal = 1
          )
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_close_reason_id"), table_name="projects")
    op.drop_column("projects", "closed_date")
    op.drop_column("projects", "competitor")
    op.drop_column("projects", "close_reason_id")
    op.drop_table("close_reasons")
    op.drop_column("project_statuses", "outcome")
