"""Per-project access control helpers.

The single source of truth for "which projects can this user see" and "can this
user share this project". These helpers are used by every UI read/write surface
so the rules live in one place.

Visibility, in order:
  - **External viewers** — only projects explicitly granted via ``ProjectGrant``.
  - **Admins** — every project, always.
  - **SEs / managers** — every project when region enforcement is OFF
    (the master switch ``system_config.region_enforcement_enabled``); when ON,
    only projects in their assigned regions (see ``user_regions``), plus any
    project directly assigned to them as the sales engineer (so you never lose
    sight of your own work, even on a region mismatch).
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AppUser, Project, ProjectGrant, ProjectNote
from app.services import system_config
from app.services.regions import get_user_region_ids


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


def region_scoped(user: AppUser) -> bool:
    """Whether hard region boundaries apply to this user right now.

    True only for internal, non-admin users (SEs + managers) while the
    region-enforcement master switch is on. Admins bypass regions (see all), and
    external viewers are governed by grants, not regions.
    """
    if user.is_external or user.is_admin:
        return False
    return system_config.region_enforcement_enabled()


def allowed_region_ids(db: Session, user: AppUser) -> set[int] | None:
    """Region ids a user may access; ``None`` means "all regions" (no limit).

    ``None`` for admins and for anyone not currently region-scoped (enforcement
    off, or external). Otherwise the user's ``user_regions`` membership set,
    which may be empty (a region-scoped user with no regions sees no projects
    except their own assignments).
    """
    if not region_scoped(user):
        return None
    return get_user_region_ids(db, user.id)


def _external_project_ids(db: Session, user: AppUser) -> set[int]:
    rows = (
        db.query(ProjectGrant.project_id)
        .filter(ProjectGrant.user_id == user.id)
        .all()
    )
    return {pid for (pid,) in rows}


def accessible_project_ids(db: Session, user: AppUser) -> set[int] | None:
    """Project ids the user may view. ``None`` means "all projects, no filter".

    - External viewers: the (possibly empty) set of granted project ids.
    - Admins, and everyone when region enforcement is off: ``None`` (all).
    - Region-scoped SEs/managers: projects in their regions ∪ their own
      assignments. A project with no region is hidden from them (admins only)
      until it's backfilled into a region.
    """
    if user.is_external:
        return _external_project_ids(db, user)
    if not region_scoped(user):
        return None

    region_ids = get_user_region_ids(db, user.id)
    conditions = [Project.sales_engineer_id == user.id]
    if region_ids:
        conditions.append(Project.region_id.in_(region_ids))
    rows = db.query(Project.id).filter(or_(*conditions)).all()
    return {pid for (pid,) in rows}


def can_view_project(db: Session, user: AppUser, project: Project) -> bool:
    """Whether the user may view a specific project."""
    if user.is_external:
        return (
            db.query(ProjectGrant.id)
            .filter(
                ProjectGrant.user_id == user.id,
                ProjectGrant.project_id == project.id,
            )
            .first()
            is not None
        )
    if not region_scoped(user):
        return True
    # Region-scoped: your own assignment is always visible; otherwise the
    # project must live in one of your regions (a region-less project is not).
    if project.sales_engineer_id == user.id:
        return True
    if project.region_id is None:
        return False
    return project.region_id in get_user_region_ids(db, user.id)


def can_edit_project(db: Session, user: AppUser, project: Project) -> bool:
    """Whether the user may modify this project or its sub-resources.

    External viewers are read-only. Admins and internal users (when region
    enforcement is off) may edit any project. Region-scoped SEs/managers may edit
    exactly the projects they can see — those in their regions, plus their own
    assignments — so visibility and edit rights stay in lock-step.
    """
    if user.is_external:
        return False
    if not region_scoped(user):
        return True
    return can_view_project(db, user, project)


def can_use_region(db: Session, user: AppUser, region_id: int | None) -> bool:
    """Whether the user may place/keep a project in ``region_id``.

    Used to guard create/reassign: a region-scoped user can only land a project
    in one of their own regions (so they can't create or move a POC out of their
    reach, or into a region — or region-less state — they don't own). Admins and
    non-enforced users may use any region.
    """
    allowed = allowed_region_ids(db, user)
    if allowed is None:
        return True
    return region_id in allowed


def can_grant_project(db: Session, user: AppUser, project: Project) -> bool:
    """Whether the user may grant/revoke access on a project.

    Admins can share any project; a project's own SE can always share it. Under
    region enforcement, a manager/SE can also share any project they can edit
    within their regions. External viewers never can.
    """
    if user.is_external:
        return False
    if user.is_admin or project.sales_engineer_id == user.id:
        return True
    if region_scoped(user):
        return can_edit_project(db, user, project)
    return False
