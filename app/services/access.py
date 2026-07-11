"""Per-project access control helpers.

The single source of truth for "which projects can this user see" and "can this
user share this project". Internal users (admins and standard users) see every
project; external viewers see only projects explicitly granted to them via
``ProjectGrant``. These helpers are used by every UI read/write surface so the
rules live in one place.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AppUser, Project, ProjectGrant, ProjectNote


def visible_project_notes(project: Project, user: AppUser) -> list[ProjectNote]:
    """Journal notes on ``project`` the user is allowed to see.

    Internal users see every note; external (viewer) users don't see notes
    marked ``is_internal_only``. Use this anywhere notes are rendered or
    exported so internal-only content never reaches an external viewer —
    on-screen, in the PDF/report, or in the artifacts zip.
    """
    if user.is_internal:
        return list(project.note_entries)
    return [n for n in project.note_entries if not n.is_internal_only]


def notes_for_report(
    project: Project, user: AppUser, *, include_internal: bool
) -> list[ProjectNote]:
    """Journal notes to render in a report, honoring the report's audience.

    Unlike :func:`visible_project_notes` (which keys off the viewer's identity
    for the on-screen page), a report has an explicit *audience*: a client-facing
    report excludes internal-only notes even for an internal author, while an
    internal report includes them. ``include_internal`` is honored only for
    internal users — an external viewer never receives internal-only notes no
    matter what audience is requested, so this stays a safe single choke point
    for reports, PDFs, and the artifacts zip.
    """
    if include_internal and user.is_internal:
        return list(project.note_entries)
    return [n for n in project.note_entries if not n.is_internal_only]


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
