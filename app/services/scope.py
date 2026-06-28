"""Per-user "My POCs vs All POCs" view scope.

This is a *view default*, not access control. Internal users (admins + standard)
may always view every project; this just controls what the dashboard, project
list, and search show by default. New users default to "mine" — the projects
where they are the assigned sales engineer — and can flip to "all". The choice
is sticky, stored alongside the user's dashboard preferences.

Access control still lives in ``app.services.access``: external viewers only
ever see projects granted to them, and scope does not apply to them.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AppUser, DashboardPref, Project
from app.services.access import accessible_project_ids

SCOPE_MINE = "mine"
SCOPE_ALL = "all"
VALID_SCOPES = {SCOPE_MINE, SCOPE_ALL}
DEFAULT_SCOPE = SCOPE_MINE


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
    return scope if scope in VALID_SCOPES else DEFAULT_SCOPE


def set_scope(db: Session, user: AppUser, scope: str) -> None:
    """Persist the user's scope, preserving other dashboard preferences."""
    if scope not in VALID_SCOPES:
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
    if requested in VALID_SCOPES:
        if requested != get_scope(db, user):
            set_scope(db, user, requested)
        return requested
    return get_scope(db, user)


def scoped_project_ids(db: Session, user: AppUser, scope: str) -> set[int] | None:
    """Project ids to show for this user at the given scope.

    Returns ``None`` to mean "no filter, show all". External viewers ignore
    scope entirely and fall back to their granted projects. Internal users get
    ``None`` for "all", or the set of projects they're the sales engineer on for
    "mine".
    """
    if user.is_external:
        return accessible_project_ids(db, user)
    if scope != SCOPE_MINE:
        return None
    rows = (
        db.query(Project.id)
        .filter(Project.sales_engineer_id == user.id)
        .all()
    )
    return {pid for (pid,) in rows}
