"""add app_config.brandfetch_client_id

Stores the Brandfetch Logo API client ID an admin sets from Settings → System, so
"Fetch from website" can pull high-quality customer logos without a redeploy. The
Brandfetch client ID is a publishable value (it rides in public image URLs), so it
is stored in plain text rather than Fernet-encrypted like vendor secrets.

Nullable ADD COLUMN — the existing singleton config row backfills to NULL, at
which point the resolver falls back to the POCT_BRANDFETCH_CLIENT_ID env var and
then to the keyless favicon source. ``app_config`` has no FTS triggers, so this is
safe.

Revision ID: 0048_add_brandfetch_client_id
Revises: 0047_search_normalize_separators
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_add_brandfetch_client_id"
down_revision: str | Sequence[str] | None = "0047_search_normalize_separators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_config",
        sa.Column("brandfetch_client_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_config", "brandfetch_client_id")
