"""add dynamic-RBAC tables: capabilities, roles, role_capabilities, user_roles

Phase 2 of the role-builder plan (docs/rbac-role-builder-plan.md). Four brand-new
tables for admin-defined roles:

- ``capabilities``      — reference mirror of the code registry (PK = the
                          resource.action slug); reconciled at startup.
- ``roles``             — admin-configurable capability bundles, with the
                          protected/seeded flags (is_system/is_superuser/
                          is_external).
- ``role_capabilities`` — role → permission map (composite PK).
- ``user_roles``        — user → role assignment (composite PK; multiple per
                          user).

All are new tables created with ``op.create_table`` and link only to
``app_users``/``roles``/``capabilities`` — the ``projects`` FTS-trigger
(``batch_alter_table``) caveat does not apply here. No seeding happens in this
migration; the four default roles + user backfill land in Phase 4.

Revision ID: 0041_add_rbac_roles
Revises: 0040_add_region_enforcement_flag
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_add_rbac_roles"
down_revision: str | Sequence[str] | None = "0040_add_region_enforcement_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("area", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=400), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_external", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_roles_key"),
    )
    op.create_index(op.f("ix_roles_key"), "roles", ["key"], unique=False)

    op.create_table(
        "role_capabilities",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("capability_key", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["capability_key"], ["capabilities.key"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("role_id", "capability_key"),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )


def downgrade() -> None:
    op.drop_table("user_roles")
    op.drop_table("role_capabilities")
    op.drop_index(op.f("ix_roles_key"), table_name="roles")
    op.drop_table("roles")
    op.drop_table("capabilities")
