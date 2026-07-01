"""Task Manager REST endpoints (admin-wide, explicit-owner model).

Tasks are per-user owned, but the REST API authenticates as a machine identity
(API key / OAuth client), not a logged-in user. So these endpoints operate
across all users and take an explicit ``owner`` (username or id) on create/update
— matching how the MCP server and other integrations authenticate today. A later
phase can bind a key to a specific user and default the owner from it.

Statuses and priorities may be passed by name or id. Task ``details`` accepts
limited HTML and is sanitized (the same allow-list as project notes), with a
plain-text rendering stored alongside for search/export.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Project, Task, TaskPriority, TaskStatus
from app.schemas.task import TaskCreate, TaskOut, TaskUpdate
from app.services import google_tasks_sync, system_config
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal
from app.services.rich_text import html_to_text, sanitize_note_html

log = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def require_tasks_module() -> None:
    """404 the task endpoints when an admin has disabled the module."""
    if not system_config.tasks_enabled():
        raise HTTPException(status_code=404, detail="The Task Manager is disabled.")


# ---------------------------------------------------------------------------
# Resolution helpers (name-or-id, like the rest of the API/MCP)
# ---------------------------------------------------------------------------


def _resolve_owner(db: Session, value: Any) -> int:
    """Resolve an owner (user id or username) to an app_user id."""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        uid = int(value)
        user = db.get(AppUser, uid)
        if user is None:
            raise HTTPException(status_code=422, detail=f"Unknown owner id: {uid}.")
        return uid
    name = str(value).strip().lower()
    user = (
        db.query(AppUser)
        .filter(AppUser.username.ilike(name))
        .one_or_none()
    )
    if user is None:
        raise HTTPException(status_code=422, detail=f"Unknown owner username: {value!r}.")
    return user.id


def _resolve_status(db: Session, value: Any) -> int:
    """Resolve a status (id or name) to a task_status id."""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        sid = int(value)
        if db.get(TaskStatus, sid) is None:
            raise HTTPException(status_code=422, detail=f"Unknown task status id: {sid}.")
        return sid
    name = str(value).strip().lower()
    row = db.query(TaskStatus).filter(TaskStatus.name.ilike(name)).one_or_none()
    if row is None:
        raise HTTPException(status_code=422, detail=f"Unknown task status: {value!r}.")
    return row.id


def _resolve_priority(db: Session, value: Any) -> int | None:
    """Resolve a priority (id or name) to a task_priority id, or None."""
    if value is None:
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        pid = int(value)
        if db.get(TaskPriority, pid) is None:
            raise HTTPException(status_code=422, detail=f"Unknown task priority id: {pid}.")
        return pid
    name = str(value).strip().lower()
    row = db.query(TaskPriority).filter(TaskPriority.name.ilike(name)).one_or_none()
    if row is None:
        raise HTTPException(status_code=422, detail=f"Unknown task priority: {value!r}.")
    return row.id


def _default_status_id(db: Session) -> int:
    """The first active status (by sort order) — used when create omits status."""
    row = (
        db.query(TaskStatus)
        .filter(TaskStatus.is_active.is_(True))
        .order_by(TaskStatus.sort_order)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="No task statuses are configured. Add one first.",
        )
    return row.id


def _validate_project(db: Session, project_id: int | None) -> int | None:
    if project_id is None:
        return None
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail=f"Unknown project id: {project_id}.")
    return project_id


def _get_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _task_event(principal: Principal, task: Task, event: str, verb: str) -> None:
    record_event(
        category="task",
        event_type=f"task.{event}",
        **principal_actor(principal),
        target_type="task",
        target_id=task.id,
        target_label=task.title,
        message=f"{verb} task '{task.title}'",
        detail={"surface": "api", "owner_user_id": task.owner_user_id},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[TaskOut])
def list_tasks(
    owner: str | None = None,
    status_id: int | None = None,
    priority_id: int | None = None,
    project_id: int | None = None,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
    _mod: None = Depends(require_tasks_module),
) -> Any:
    """List tasks across all users, newest-updated first.

    Filter by ``owner`` (username or id), ``status_id``, ``priority_id``,
    ``project_id``, and ``include_archived``.
    """
    q = db.query(Task)
    if owner is not None:
        q = q.filter(Task.owner_user_id == _resolve_owner(db, owner))
    if status_id is not None:
        q = q.filter(Task.status_id == status_id)
    if priority_id is not None:
        q = q.filter(Task.priority_id == priority_id)
    if project_id is not None:
        q = q.filter(Task.project_id == project_id)
    if not include_archived:
        q = q.filter(Task.is_archived.is_(False))
    return q.order_by(Task.updated_at.desc()).all()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
    _mod: None = Depends(require_tasks_module),
) -> Any:
    """Get one task in full."""
    return _get_task(db, task_id)


@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(
    body: TaskCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
    _mod: None = Depends(require_tasks_module),
) -> Any:
    """Create a task for a user. ``owner`` (username or id) and ``title`` required."""
    owner_id = _resolve_owner(db, body.owner)
    status_id = (
        _resolve_status(db, body.status) if body.status is not None
        else _default_status_id(db)
    )
    details_html = sanitize_note_html(body.details)
    task = Task(
        owner_user_id=owner_id,
        title=body.title,
        status_id=status_id,
        priority_id=_resolve_priority(db, body.priority),
        project_id=_validate_project(db, body.project_id),
        start_date=body.start_date,
        due_date=body.due_date,
        details=html_to_text(details_html),
        details_html=details_html,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _task_event(principal, task, "created", "Created")
    google_tasks_sync.sync_after_change(db, task)
    return task


@router.patch("/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int,
    body: TaskUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
    _mod: None = Depends(require_tasks_module),
) -> Any:
    """Update a task. Only provided fields change; ``owner`` reassigns ownership."""
    task = _get_task(db, task_id)
    fields = body.model_dump(exclude_unset=True)

    if "owner" in fields:
        task.owner_user_id = _resolve_owner(db, fields["owner"])
    if "title" in fields and fields["title"] is not None:
        task.title = fields["title"]
    if "status" in fields and fields["status"] is not None:
        task.status_id = _resolve_status(db, fields["status"])
    if "priority" in fields:
        task.priority_id = _resolve_priority(db, fields["priority"])
    if "project_id" in fields:
        task.project_id = _validate_project(db, fields["project_id"])
    if "start_date" in fields:
        task.start_date = fields["start_date"]
    if "due_date" in fields:
        task.due_date = fields["due_date"]
    if "details" in fields:
        details_html = sanitize_note_html(fields["details"])
        task.details_html = details_html
        task.details = html_to_text(details_html)
    if "is_archived" in fields and fields["is_archived"] is not None:
        task.is_archived = fields["is_archived"]
        task.archived_at = datetime.now(UTC) if task.is_archived else None

    db.commit()
    db.refresh(task)
    _task_event(principal, task, "updated", "Updated")
    google_tasks_sync.sync_after_change(db, task)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
    _mod: None = Depends(require_tasks_module),
) -> None:
    """Delete a task."""
    task = _get_task(db, task_id)
    title = task.title
    tid = task.id
    owner_id = task.owner_user_id
    external_id = task.external_id
    db.delete(task)
    db.commit()
    google_tasks_sync.push_delete(db, owner_id, external_id)
    record_event(
        category="task",
        event_type="task.deleted",
        **principal_actor(principal),
        target_type="task",
        target_id=tid,
        target_label=title,
        message=f"Deleted task '{title}'",
        detail={"surface": "api", "owner_user_id": owner_id},
    )
