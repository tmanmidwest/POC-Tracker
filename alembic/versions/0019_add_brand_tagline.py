"""add editable brand tagline (sub-header)

Adds ``app_branding.brand_tagline`` — the small sub-header shown under the brand
name on the sidebar and login page (default "POC · non-production"). Existing
rows are backfilled with the default so nothing visibly changes until edited.

Revision ID: 0019_brand_tagline
Revises: 0018_library_set_default
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_brand_tagline"
down_revision: str | Sequence[str] | None = "0018_library_set_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_TAGLINE = "POC · non-production"


def upgrade() -> None:
    op.add_column(
        "app_branding",
        sa.Column(
            "brand_tagline",
            sa.String(length=100),
            nullable=False,
            server_default=_DEFAULT_TAGLINE,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("app_branding", schema=None) as batch_op:
        batch_op.drop_column("brand_tagline")
