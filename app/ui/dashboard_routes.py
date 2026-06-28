"""HTML UI dashboard — projects grouped by status, with per-user view prefs."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, DashboardPref, Project, ProjectStatus
from app.services.scope import get_scope, resolve_scope, scoped_project_ids
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/dashboard", tags=["ui"], include_in_schema=False)


# All optional columns the user can toggle on the dashboard cards.
ALL_COLUMNS = [
    {"key": "name", "label": "Project"},
    {"key": "sales_engineer", "label": "Sales Engineer"},
    {"key": "account_executive", "label": "Account Exec"},
    {"key": "salesforce", "label": "Salesforce Opp"},
    {"key": "notebook", "label": "Notebook Link"},
    {"key": "poc_instance", "label": "POC Instance"},
    {"key": "start_date", "label": "Start"},
    {"key": "end_date", "label": "End"},
    {"key": "progress", "label": "Use-case progress"},
]
DEFAULT_COLUMNS = ["name", "sales_engineer", "salesforce", "notebook", "poc_instance", "start_date", "end_date", "progress"]
DEFAULT_SORT = "updated"  # updated | start_date | name


def _load_prefs(db: Session, user: AppUser) -> dict[str, Any]:
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    prefs: dict[str, Any] = {
        "columns": DEFAULT_COLUMNS,
        "status_ids": None,  # None = show all
        "status_order": None,  # None = use ProjectStatus.sort_order
        "sort": DEFAULT_SORT,
    }
    if row and row.config_json:
        try:
            stored = json.loads(row.config_json)
            prefs.update({k: v for k, v in stored.items() if v is not None})
        except (ValueError, TypeError):
            log.warning("dashboard_prefs_parse_failed", extra={"user": user.username})
    return prefs


def _order_statuses(
    statuses: list[ProjectStatus], order: list[int] | None
) -> list[ProjectStatus]:
    """Order statuses by the user's saved order; any not listed fall to the end
    keeping their canonical sort_order."""
    if not order:
        return statuses
    pos = {sid: i for i, sid in enumerate(order)}
    return sorted(statuses, key=lambda s: (pos.get(s.id, len(order)), s.sort_order))


def _progress(project: Project) -> dict[str, int]:
    total = len(project.use_cases)
    done = sum(
        1 for uc in project.use_cases if uc.status and uc.status.is_complete_status
    )
    pct = round(done / total * 100) if total else 0
    return {"total": total, "done": done, "pct": pct}


@router.get("")
@router.get("/")
def dashboard(
    request: Request,
    scope: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()

    selected_status_ids = prefs.get("status_ids")
    visible_statuses = [
        s for s in statuses
        if selected_status_ids is None or s.id in selected_status_ids
    ]
    visible_statuses = _order_statuses(visible_statuses, prefs.get("status_order"))

    # "My POCs" (default) vs "All POCs"; external viewers ignore scope and only
    # ever see projects shared with them.
    scope = resolve_scope(db, user, scope)
    visible_ids = scoped_project_ids(db, user, scope)

    sort = prefs.get("sort", DEFAULT_SORT)
    groups = []
    for status in visible_statuses:
        q = (
            db.query(Project)
            .filter(Project.status_id == status.id, Project.is_archived.is_(False))
        )
        if visible_ids is not None:
            q = q.filter(Project.id.in_(visible_ids))
        if sort == "start_date":
            q = q.order_by(Project.start_date.is_(None), Project.start_date)
        elif sort == "name":
            q = q.order_by(Project.name)
        else:
            q = q.order_by(Project.updated_at.desc())
        projects = q.all()
        groups.append(
            {
                "status": status,
                "projects": [
                    {"project": p, "progress": _progress(p)} for p in projects
                ],
            }
        )

    total_q = db.query(Project).filter(Project.is_archived.is_(False))
    if visible_ids is not None:
        total_q = total_q.filter(Project.id.in_(visible_ids))
    total_active = total_q.count()
    return render(
        request,
        "dashboard/index.html",
        current_user=user,
        active_section="dashboard",
        groups=groups,
        prefs=prefs,
        all_columns=ALL_COLUMNS,
        total_active=total_active,
        scope=scope,
    )


@router.get("/preferences")
def preferences_form(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    statuses = _order_statuses(statuses, prefs.get("status_order"))
    return render(
        request,
        "dashboard/preferences.html",
        current_user=user,
        active_section="dashboard",
        prefs=prefs,
        statuses=statuses,
        all_columns=ALL_COLUMNS,
    )


@router.post("/preferences")
async def save_preferences(
    request: Request,
    sort: str = Form(DEFAULT_SORT),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    form = await request.form()
    columns = [c["key"] for c in ALL_COLUMNS if form.get(f"col_{c['key']}")]
    status_values = form.getlist("status_ids")  # type: ignore[attr-defined]
    status_ids = [int(s) for s in status_values] if status_values else None

    order_raw = form.get("status_order", "")
    status_order = [int(x) for x in str(order_raw).split(",") if x.strip().isdigit()] or None

    config = {
        "columns": columns or DEFAULT_COLUMNS,
        "status_ids": status_ids,
        "status_order": status_order,
        "sort": sort if sort in {"updated", "start_date", "name"} else DEFAULT_SORT,
        # Preserve the user's My/All POC scope, which lives in the same blob but
        # is toggled from the dashboard rather than this preferences form.
        "scope": get_scope(db, user),
    }
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    if row is None:
        row = DashboardPref(app_user_id=user.id)
        db.add(row)
    row.config_json = json.dumps(config)
    db.commit()
    flash(request, "Dashboard preferences saved.", "success")
    return RedirectResponse(url="/ui/dashboard", status_code=303)
