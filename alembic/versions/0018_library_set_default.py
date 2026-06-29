"""pin an explicit default library + rename the seeded one

Adds ``library_sets.is_default`` so the primary library is an explicit,
delete-protected landing/fallback (instead of "alphabetically first active").
Renames the seeded "Standard" library to "Core Use Case Library" and pins the
first library as the default.

Revision ID: 0018_library_set_default
Revises: 0017_library_sets
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_library_set_default"
down_revision: str | Sequence[str] | None = "0017_library_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "library_sets",
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # Rename the untouched seeded library to its clearer name.
    op.execute(
        "UPDATE library_sets SET name = 'Core Use Case Library' WHERE name = 'Standard'"
    )
    # Pin the first library (the seeded one) as the default, regardless of name.
    op.execute(
        "UPDATE library_sets SET is_default = 1 "
        "WHERE id = (SELECT MIN(id) FROM library_sets)"
    )


def downgrade() -> None:
    with op.batch_alter_table("library_sets", schema=None) as batch_op:
        batch_op.drop_column("is_default")
    op.execute(
        "UPDATE library_sets SET name = 'Standard' WHERE name = 'Core Use Case Library'"
    )
