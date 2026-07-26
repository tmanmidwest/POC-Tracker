"""SavedReport model — a user-defined, saved, shareable, schedulable report.

One row is one report: what entity it runs over (``project`` | ``task`` |
``use_case``), the declarative ``definition`` (columns / filters / group-by /
measures / sort, validated against ``app.services.reporting.registry`` before it
is ever stored), and how it's shared.

The same table backs both product use cases the reporting plan calls out,
distinguished only by ``visibility``:

- ``private``   — only the owner sees and runs it.
- ``shared``    — visible to an ``audience`` of roles and/or regions.
- ``published`` — added to a curated catalog for a client/internal audience.

Whatever the visibility, a report is **always re-run through the viewer's region
access** at run time (see ``app.services.reporting.engine``), so a shared or
published report can never leak rows across regions — the row set is recomputed
per viewer, never stored.

The JSON blobs use native ``JSONB`` (the app is Postgres-only), so definitions
are queryable/indexable in SQL later without a schema change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.app_user import AppUser

# Visibility values.
VISIBILITY_PRIVATE = "private"
VISIBILITY_SHARED = "shared"
VISIBILITY_PUBLISHED = "published"
VALID_VISIBILITY = (VISIBILITY_PRIVATE, VISIBILITY_SHARED, VISIBILITY_PUBLISHED)

# Published-audience kinds — mirrors the per-project report audience concept so an
# internal-only field never surfaces to a client audience.
AUDIENCE_CLIENT = "client"
AUDIENCE_INTERNAL = "internal"


class SavedReport(Base, TimestampMixin):
    """A saved, user-defined report definition."""

    __tablename__ = "saved_reports"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The author/owner. SET NULL (not cascade) so a published or shared report
    # outlives the account that created it; a private orphan simply stops
    # appearing for anyone.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Registry entity key: 'project' | 'task' | 'use_case'.
    entity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Tabular (rows) vs summary (grouped + aggregated).
    is_summary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # The declarative definition — always validated against the registry before
    # being written here (see reporting.registry.validate_definition):
    #   {columns:[...], filters:[{field,op,value}], group_by, measures:[...], sort:[...]}
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # 'private' | 'shared' | 'published'.
    visibility: Mapped[str] = mapped_column(
        String(12), nullable=False, default=VISIBILITY_PRIVATE,
        server_default=VISIBILITY_PRIVATE, index=True,
    )

    # Audience, by visibility:
    #   shared    -> {"role_keys": [...], "region_ids": [...]}
    #   published -> {"kind": "client" | "internal"}
    #   private   -> null
    audience: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Nullable schedule (Phase 3). When present + active, the scheduler loop
    # renders and emails it:
    #   {"active": bool, "freq": "daily"|"weekly"|"monthly", "day": int,
    #    "hour": int, "recipients": [email, ...], "format": "xlsx"|"csv"|"pdf",
    #    "last_sent_on": "YYYY-MM-DD" | null}
    schedule: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    owner: Mapped[AppUser | None] = relationship("AppUser", lazy="joined")

    @property
    def audience_kind(self) -> str:
        """Published audience kind ('client'|'internal'); 'internal' by default."""
        if isinstance(self.audience, dict):
            kind = self.audience.get("kind")
            if kind == AUDIENCE_CLIENT:
                return AUDIENCE_CLIENT
        return AUDIENCE_INTERNAL

    @property
    def is_scheduled(self) -> bool:
        return bool(self.schedule and self.schedule.get("active"))

    def __repr__(self) -> str:
        return f"<SavedReport id={self.id} entity={self.entity!r} name={self.name!r}>"
