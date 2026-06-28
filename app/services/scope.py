"""Per-user project view scope ("My Projects", "All Projects", a teammate, …).

This is a *view default*, not access control. Internal users (admins + standard)
may always view every project; this just controls what the dashboard, project
list, and search show by default. New users default to "mine" — the projects
where they are the assigned sales engineer — and can flip to other scopes. The
choice is sticky, stored alongside the user's dashboard preferences.

A scope is one of:
  - ``"mine"``        — projects where the current user is the sales engineer
  - ``"all"``         — every project (no filter)
  - ``"unassigned"``  — projects with no sales engineer
  - ``"user:<id>"``   — projects assigned to a specific sales engineer

Access control still lives in ``app.services.access``: external viewers only
ever see projects granted to them, and scope does not apply to them.
"""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from app.models import AppUser, DashboardPref, Project
from app.services.access import accessible_project_ids

SCOPE_MINE = "mine"
SCOPE_ALL = "all"
SCOPE_UNASSIGNED = "unassigned"
FIXED_SCOPES = {SCOPE_MINE, SCOPE_ALL, SCOPE_UNASSIGNED}
DEFAULT_SCOPE = SCOPE_MINE

# "user:<digits>" — projects assigned to a specific sales engineer.
_USER_SCOPE_RE = re.compile(r"^user:(\d+)$")


def is_valid_scope(scope: str | None) -> bool:
    """Whether ``scope`` is a recognized scope token."""
    return bool(scope) and (scope in FIXED_SCOPES or _USER_SCOPE_RE.match(scope) is not None)


def _config(db: Session, user: AppUser) -> dict:
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    if row and row.config_json:
        try:
            cfg = json.loads(row.config_json)
            if isinstance(cfg, dict):
                return cfg
        except (ValueError, TypeError):
            pass
    return {}


def get_scope(db: Session, user: AppUser) -> str:
    """The user's stored scope, defaulting to "mine"."""
    scope = _config(db, user).get("scope")
    return scope if is_valid_scope(scope) else DEFAULT_SCOPE


def set_scope(db: Session, user: AppUser, scope: str) -> None:
    """Persist the user's scope, preserving other dashboard preferences."""
    if not is_valid_scope(scope):
        return
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    cfg: dict = {}
    if row and row.config_json:
        try:
            loaded = json.loads(row.config_json)
            if isinstance(loaded, dict):
                cfg = loaded
        except (ValueError, TypeError):
            cfg = {}
    cfg["scope"] = scope
    if row is None:
        row = DashboardPref(app_user_id=user.id)
        db.add(row)
    row.config_json = json.dumps(cfg)
    db.commit()


def resolve_scope(db: Session, user: AppUser, requested: str | None) -> str:
    """Return the scope to use, persisting ``requested`` when it's a valid change.

    Pass the ``?scope=`` query param as ``requested``: a valid value sticks for
    next time; anything else falls back to the stored (or default) scope.
    """
    if is_valid_scope(requested):
        if requested != get_scope(db, user):
            set_scope(db, user, requested)  # type: ignore[arg-type]
        return requested  # type: ignore[return-value]
    return get_scope(db, user)


def scoped_project_ids(db: Session, user: AppUser, scope: str) -> set[int] | None:
    """Project ids to show for this user at the given scope.

    Returns ``None`` to mean "no filter, show all". External viewers ignore
    scope entirely and fall back to their granted projects.
    """
    if user.is_external:
        return accessible_project_ids(db, user)
    if scope == SCOPE_ALL:
        return None

    q = db.query(Project.id)
    if scope == SCOPE_UNASSIGNED:
        q = q.filter(Project.sales_engineer_id.is_(None))
    else:
        m = _USER_SCOPE_RE.match(scope or "")
        target_id = int(m.group(1)) if m else user.id  # default "mine"
        q = q.filter(Project.sales_engineer_id == target_id)
    return {pid for (pid,) in q.all()}


def selectable_engineers(db: Session, user: AppUser) -> list[AppUser]:
    """Internal teammates the user can filter by (excludes self — "My Projects"
    already covers that — and external viewers, who are never sales engineers)."""
    return (
        db.query(AppUser)
        .filter(
            AppUser.is_active.is_(True),
            AppUser.is_external.is_(False),
            AppUser.id != user.id,
        )
        .order_by(AppUser.username)
        .all()
    )
