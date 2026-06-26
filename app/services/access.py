"""Per-project access control helpers.

The single source of truth for "which projects can this user see" and "can this
user share this project". Internal users (admins and standard users) see every
project; external viewers see only projects explicitly granted to them via
``ProjectGrant``. These helpers are used by every UI read/write surface so the
rules live in one place.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppUser, Project, ProjectGrant


def accessible_project_ids(db: Session, user: AppUser) -> set[int] | None:
    """Project ids the user may view.

    Returns ``None`` for internal users — meaning "all projects, apply no
    filter". For external viewers, returns the (possibly empty) set of granted
    project ids.
    """
    if user.is_internal:
        return None
    rows = (
        db.query(ProjectGrant.project_id)
        .filter(ProjectGrant.user_id == user.id)
        .all()
    )
    return {pid for (pid,) in rows}


def can_view_project(db: Session, user: AppUser, project: Project) -> bool:
    """Whether the user may view a specific project."""
    if user.is_internal:
        return True
    return (
        db.query(ProjectGrant.id)
        .filter(
            ProjectGrant.user_id == user.id,
            ProjectGrant.project_id == project.id,
        )
        .first()
        is not None
    )


def can_grant_project(user: AppUser, project: Project) -> bool:
    """Whether the user may grant/revoke access on a project.

    Admins can share any project; a project's assigned sales engineer can share
    their own. External viewers never can.
    """
    if user.is_external:
        return False
    return user.is_admin or project.sales_engineer_id == user.id
