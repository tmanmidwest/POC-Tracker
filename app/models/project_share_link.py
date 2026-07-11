"""Public customer-facing status link for a project.

A share link exposes a *read-only, no-login* status page for one project at a
hard-to-guess URL (``/portal/<token>``). It's the customer-facing counterpart to
:class:`ProjectGrant` (which scopes an authenticated external viewer): here there
is no account at all — anyone with the link sees a polished progress page.

Safety model:

* One row per project (``project_id`` is unique). Absent row = never shared.
* ``is_enabled`` gates visibility without destroying the token; toggling it off
  makes the link 404 immediately, toggling on restores the *same* URL.
* ``token`` is a 256-bit ``secrets.token_urlsafe`` value; rotating it mints a new
  one and instantly dead-links the old URL.
* The portal renderer only ever shows customer-safe content: use-case progress
  and non-internal notes. Internal-only notes, tasks, and internal links
  (Salesforce/notebook/instance URLs) never reach this surface.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.project import Project


def generate_token() -> str:
    """A URL-safe, high-entropy (256-bit) share token."""
    return secrets.token_urlsafe(32)


class ProjectShareLink(Base, TimestampMixin):
    """A public, read-only status link for one project."""

    __tablename__ = "project_share_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True, default=generate_token
    )
    # Off makes the link 404 without discarding the token (reversible).
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Username of whoever created the link (audit convenience).
    created_by: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # Lightweight view telemetry, shown in the share panel.
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    project: Mapped[Project] = relationship("Project", lazy="joined")

    def __repr__(self) -> str:
        return f"<ProjectShareLink project_id={self.project_id} enabled={self.is_enabled}>"
