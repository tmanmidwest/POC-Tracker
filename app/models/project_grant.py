"""Per-project access grant.

A row gives one app user read access to one project. Grants exist to scope what
an *external viewer* can see. Internal users (admins, managers, standard SEs)
never need a grant: admins see every project, and standard/manager visibility is
governed by regions instead (see ``services/access`` — all projects when region
enforcement is off, their regions when it's on). Revoking access deletes the row.

Both foreign keys cascade on delete so grants disappear automatically when the
project or the user is removed.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin
from app.models.app_user import AppUser
from app.models.project import Project

# Grant tiers. Only "viewer" (read-only) is used today; "editor" is reserved so
# the column can grow without a migration.
TIER_VIEWER = "viewer"


class ProjectGrant(Base, TimestampMixin):
    """Grants one user access to one project."""

    __tablename__ = "project_grants"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_grant_project_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tier: Mapped[str] = mapped_column(String(50), nullable=False, default=TIER_VIEWER)
    # Who created the grant (audit convenience); kept even if that user is gone.
    granted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id"), nullable=True
    )

    project: Mapped[Project] = relationship("Project", lazy="joined")
    user: Mapped[AppUser] = relationship(
        "AppUser", foreign_keys=[user_id], lazy="joined"
    )
    granted_by: Mapped[AppUser | None] = relationship(
        "AppUser", foreign_keys=[granted_by_user_id]
    )

    def __repr__(self) -> str:
        return f"<ProjectGrant project_id={self.project_id} user_id={self.user_id}>"
