"""HTML UI for the per-user Task Manager.

A task dashboard (tasks grouped by status, with per-user view prefs) plus task
create/edit/archive/delete. Tasks are owned by the current user; admins may view
everyone's. Statuses and priorities are admin-managed global lookups. Mirrors the
project dashboard and note patterns.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    Project,
    Task,
    TaskDashboardPref,
    TaskPriority,
    TaskStatus,
)
from app.services import google_oauth, google_tasks_sync, system_config
from app.services.access import accessible_project_ids
from app.services.audit import record_event
from app.services.rich_text import html_to_text, sanitize_note_html
from app.services.tasks import (
    OWNER_ALL,
    OWNER_MINE,
    base_task_query,
    can_view_all_tasks,
    get_owned_task,
)
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/tasks", tags=["ui"], include_in_schema=False)


# Optional columns the user can toggle on the task dashboard (title is always shown).
ALL_COLUMNS = [
    {"key": "project", "label": "Project"},
    {"key": "priority", "label": "Priority"},
    {"key": "start_date", "label": "Start"},
    {"key": "due_date", "label": "Due"},
    {"key": "owner", "label": "Owner"},
]
DEFAULT_COLUMNS = ["project", "priority", "start_date", "due_date"]
DEFAULT_SORT = "updated"  # updated | due_date | priority | title
VALID_SORTS = {"updated", "due_date", "priority", "title"}


def require_tasks_module() -> None:
    """404 the task surfaces when the module is disabled by an admin."""
    if not system_config.tasks_enabled():
        raise HTTPException(status_code=404, detail="The Task Manager is disabled.")


def _parse_date(raw: str | None) -> date | None:
    """Parse a YYYY-MM-DD form value, or None if blank/invalid."""
    if not raw or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _load_prefs(db: Session, user: AppUser) -> dict[str, Any]:
    row = (
        db.query(TaskDashboardPref)
        .filter(TaskDashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    prefs: dict[str, Any] = {
        "columns": DEFAULT_COLUMNS,
        "status_ids": None,  # None = show all
        "priority_ids": None,  # None = show all
        "sort": DEFAULT_SORT,
        "owner": OWNER_MINE,
        "show_archived": False,
    }
    if row and row.config_json:
        try:
            stored = json.loads(row.config_json)
            prefs.update({k: v for k, v in stored.items() if v is not None})
        except (ValueError, TypeError):
            log.warning("task_prefs_parse_failed", extra={"user": user.username})
    # Non-admins can never use the "all users" scope.
    if prefs.get("owner") == OWNER_ALL and not can_view_all_tasks(user):
        prefs["owner"] = OWNER_MINE
    return prefs


def _task_event(request: Request, user: AppUser, task: Task, event: str, verb: str) -> None:
    record_event(
        category="task",
        event_type=f"task.{event}",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="task",
        target_id=task.id,
        target_label=task.title,
        message=f"{verb} task '{task.title}'",
        detail={"surface": "ui"},
        request=request,
    )


def _apply_sort(query: Any, sort: str) -> Any:
    if sort == "due_date":
        return query.order_by(Task.due_date.is_(None), Task.due_date)
    if sort == "title":
        return query.order_by(Task.title)
    if sort == "priority":
        # Higher priority (lower sort_order) first; unprioritized last.
        return query.outerjoin(
            TaskPriority, Task.priority_id == TaskPriority.id
        ).order_by(Task.priority_id.is_(None), TaskPriority.sort_order)
    return query.order_by(Task.updated_at.desc())


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("")
@router.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(TaskStatus).order_by(TaskStatus.sort_order).all()

    selected_status_ids = prefs.get("status_ids")
    visible_statuses = [
        s for s in statuses
        if selected_status_ids is None or s.id in selected_status_ids
    ]

    owner = prefs.get("owner", OWNER_MINE)
    sort = prefs.get("sort", DEFAULT_SORT)
    priority_ids = prefs.get("priority_ids")
    show_archived = bool(prefs.get("show_archived"))

    groups = []
    total = 0
    for status in visible_statuses:
        q = base_task_query(db, user, owner).filter(Task.status_id == status.id)
        if not show_archived:
            q = q.filter(Task.is_archived.is_(False))
        if priority_ids:
            q = q.filter(Task.priority_id.in_(priority_ids))
        q = _apply_sort(q, sort)
        tasks = q.all()
        total += len(tasks)
        groups.append({"status": status, "tasks": tasks})

    # Google Tasks connection state (only when the admin has enabled the integration).
    google = {"ready": google_oauth.is_ready(db), "cred": None}
    if google["ready"]:
        google["cred"] = google_tasks_sync.get_credential(db, user.id)

    return render(
        request,
        "tasks/index.html",
        current_user=user,
        active_section="tasks",
        groups=groups,
        prefs=prefs,
        all_columns=ALL_COLUMNS,
        total_active=total,
        can_view_all=can_view_all_tasks(user),
        today=date.today().isoformat(),
        google=google,
    )


@router.get("/preferences")
def preferences_form(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(TaskStatus).order_by(TaskStatus.sort_order).all()
    priorities = db.query(TaskPriority).order_by(TaskPriority.sort_order).all()
    return render(
        request,
        "tasks/preferences.html",
        current_user=user,
        active_section="tasks",
        prefs=prefs,
        statuses=statuses,
        priorities=priorities,
        all_columns=ALL_COLUMNS,
        can_view_all=can_view_all_tasks(user),
    )


@router.post("/preferences")
async def save_preferences(
    request: Request,
    sort: str = Form(DEFAULT_SORT),
    owner: str = Form(OWNER_MINE),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    form = await request.form()
    columns = [c["key"] for c in ALL_COLUMNS if form.get(f"col_{c['key']}")]
    status_values = form.getlist("status_ids")  # type: ignore[attr-defined]
    status_ids = [int(s) for s in status_values] if status_values else None
    priority_values = form.getlist("priority_ids")  # type: ignore[attr-defined]
    priority_ids = [int(p) for p in priority_values] if priority_values else None

    owner_choice = OWNER_ALL if (owner == OWNER_ALL and can_view_all_tasks(user)) else OWNER_MINE

    config = {
        "columns": columns or DEFAULT_COLUMNS,
        "status_ids": status_ids,
        "priority_ids": priority_ids,
        "sort": sort if sort in VALID_SORTS else DEFAULT_SORT,
        "owner": owner_choice,
        "show_archived": bool(form.get("show_archived")),
    }
    row = (
        db.query(TaskDashboardPref)
        .filter(TaskDashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    if row is None:
        row = TaskDashboardPref(app_user_id=user.id)
        db.add(row)
    row.config_json = json.dumps(config)
    db.commit()
    flash(request, "Task view preferences saved.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


# ---------------------------------------------------------------------------
# Create / edit
# ---------------------------------------------------------------------------


def _form_context(db: Session, user: AppUser) -> dict[str, Any]:
    """Statuses, priorities, and projects available to pick on the task form."""
    statuses = (
        db.query(TaskStatus)
        .filter(TaskStatus.is_active.is_(True))
        .order_by(TaskStatus.sort_order)
        .all()
    )
    priorities = (
        db.query(TaskPriority)
        .filter(TaskPriority.is_active.is_(True))
        .order_by(TaskPriority.sort_order)
        .all()
    )
    # Projects the user may see (internal users: all; external never reach here).
    pq = db.query(Project).filter(Project.is_archived.is_(False))
    allowed = accessible_project_ids(db, user)
    if allowed is not None:
        pq = pq.filter(Project.id.in_(allowed))
    projects = pq.order_by(Project.name).all()
    return {"statuses": statuses, "priorities": priorities, "projects": projects}


@router.get("/new")
def new_form(
    request: Request,
    project_id: int | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    ctx = _form_context(db, user)
    default_status = ctx["statuses"][0].id if ctx["statuses"] else None
    return render(
        request,
        "tasks/form.html",
        current_user=user,
        active_section="tasks",
        task=None,
        form={"status_id": default_status, "project_id": project_id},
        form_action="/ui/tasks/new",
        **ctx,
    )


def _read_task_form(form: Any) -> dict[str, Any]:
    def _int(name: str) -> int | None:
        raw = form.get(name)
        try:
            return int(raw) if raw not in (None, "") else None
        except (ValueError, TypeError):
            return None

    return {
        "title": (form.get("title") or "").strip(),
        "status_id": _int("status_id"),
        "priority_id": _int("priority_id"),
        "project_id": _int("project_id"),
        "start_date": _parse_date(form.get("start_date")),
        "due_date": _parse_date(form.get("due_date")),
        "details_html_raw": form.get("details"),
        "is_internal_only": form.get("is_internal_only") is not None,
    }


def _validate_project(db: Session, user: AppUser, project_id: int | None) -> int | None:
    """Keep only a project the user may actually see; else drop the link."""
    if project_id is None:
        return None
    allowed = accessible_project_ids(db, user)
    if allowed is not None and project_id not in allowed:
        return None
    return project_id if db.get(Project, project_id) is not None else None


@router.post("/new")
async def create_task(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    form = await request.form()
    data = _read_task_form(form)

    if not data["title"]:
        flash(request, "A task title is required.", "error")
        return RedirectResponse(url="/ui/tasks/new", status_code=303)
    if data["status_id"] is None:
        flash(request, "Pick a status for the task.", "error")
        return RedirectResponse(url="/ui/tasks/new", status_code=303)

    details_html = sanitize_note_html(data["details_html_raw"])
    task = Task(
        owner_user_id=user.id,
        title=data["title"][:300],
        status_id=data["status_id"],
        priority_id=data["priority_id"],
        project_id=_validate_project(db, user, data["project_id"]),
        start_date=data["start_date"],
        due_date=data["due_date"],
        details=html_to_text(details_html),
        details_html=details_html,
        is_internal_only=data["is_internal_only"],
    )
    db.add(task)
    db.commit()
    _task_event(request, user, task, "created", "Created")
    google_tasks_sync.sync_after_change(db, task)
    flash(request, f"Task '{task.title}' created.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.get("/{task_id}/edit")
def edit_form(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    ctx = _form_context(db, user)
    form = {
        "title": task.title,
        "status_id": task.status_id,
        "priority_id": task.priority_id,
        "project_id": task.project_id,
        "start_date": task.start_date.isoformat() if task.start_date else "",
        "due_date": task.due_date.isoformat() if task.due_date else "",
        "details_html": task.details_html,
        "details": task.details,
        "is_internal_only": task.is_internal_only,
    }
    return render(
        request,
        "tasks/form.html",
        current_user=user,
        active_section="tasks",
        task=task,
        form=form,
        form_action=f"/ui/tasks/{task_id}/edit",
        **ctx,
    )


@router.post("/{task_id}/edit")
async def update_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    form = await request.form()
    data = _read_task_form(form)

    if not data["title"]:
        flash(request, "A task title is required.", "error")
        return RedirectResponse(url=f"/ui/tasks/{task_id}/edit", status_code=303)
    if data["status_id"] is None:
        flash(request, "Pick a status for the task.", "error")
        return RedirectResponse(url=f"/ui/tasks/{task_id}/edit", status_code=303)

    details_html = sanitize_note_html(data["details_html_raw"])
    task.title = data["title"][:300]
    task.status_id = data["status_id"]
    task.priority_id = data["priority_id"]
    task.project_id = _validate_project(db, user, data["project_id"])
    task.start_date = data["start_date"]
    task.due_date = data["due_date"]
    task.details = html_to_text(details_html)
    task.details_html = details_html
    task.is_internal_only = data["is_internal_only"]
    db.commit()
    _task_event(request, user, task, "updated", "Updated")
    google_tasks_sync.sync_after_change(db, task)
    flash(request, "Task saved.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.post("/{task_id}/status")
async def set_status(
    task_id: int,
    request: Request,
    status_id: int = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if db.get(TaskStatus, status_id) is None:
        raise HTTPException(status_code=400, detail="Unknown status.")
    task.status_id = status_id
    db.commit()
    _task_event(request, user, task, "status_changed", "Changed status of")
    google_tasks_sync.sync_after_change(db, task)
    # Return to wherever the change was made (dashboard or a project page).
    back = request.headers.get("referer") or "/ui/tasks"
    return RedirectResponse(url=back, status_code=303)


@router.post("/{task_id}/archive")
def archive_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.is_archived = True
    task.archived_at = datetime.now(UTC)
    db.commit()
    _task_event(request, user, task, "archived", "Archived")
    google_tasks_sync.sync_after_change(db, task)
    flash(request, "Task archived.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.post("/{task_id}/restore")
def restore_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    task.is_archived = False
    task.archived_at = None
    db.commit()
    _task_event(request, user, task, "restored", "Restored")
    google_tasks_sync.sync_after_change(db, task)
    flash(request, "Task restored.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.post("/{task_id}/delete")
def delete_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    _mod: None = Depends(require_tasks_module),
) -> Response:
    task = get_owned_task(db, task_id, user)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    title = task.title
    task_id_snapshot = task.id
    owner_id = task.owner_user_id
    external_id = task.external_id
    db.delete(task)
    db.commit()
    google_tasks_sync.push_delete(db, owner_id, external_id)
    record_event(
        category="task",
        event_type="task.deleted",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="task",
        target_id=task_id_snapshot,
        target_label=title,
        message=f"Deleted task '{title}'",
        detail={"surface": "ui"},
        request=request,
    )
    flash(request, f"Task '{title}' deleted.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)
