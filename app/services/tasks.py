"""Helpers for the per-user Task Manager.

Tasks are owned by the user who created them. These helpers centralize the
ownership rule used by every task UI surface: a user sees their own tasks;
admins may additionally view everyone's. External viewers never have task
access (their routes are gated out at the router level).
"""

from __future__ import annotations

from sqlalchemy.orm import Query, Session

from app.models import AppUser, Project, Task

# Admin "whose tasks" scopes for the dashboard.
OWNER_MINE = "mine"
OWNER_ALL = "all"


def can_view_all_tasks(user: AppUser) -> bool:
    """Whether the user may view tasks owned by others (admins only)."""
    return bool(user.is_admin)


def base_task_query(db: Session, user: AppUser, owner: str = OWNER_MINE) -> Query:
    """A Task query scoped to what ``user`` may see at the given owner scope.

    Non-admins are always restricted to their own tasks regardless of ``owner``.
    Admins may pass ``OWNER_ALL`` to see every user's tasks.
    """
    q = db.query(Task)
    if owner == OWNER_ALL and can_view_all_tasks(user):
        return q
    return q.filter(Task.owner_user_id == user.id)


def visible_project_tasks(db: Session, project: Project, user: AppUser) -> list[Task]:
    """Non-archived tasks assigned to ``project`` that ``user`` may see.

    - Internal non-admin user: their own tasks on the project.
    - Internal admin: every user's tasks on the project.
    - External viewer: every task on the project that is **not** marked
      ``is_internal_only``, regardless of owner (read-only). This is the task
      analogue of ``visible_project_notes`` — the single place that decides task
      visibility for external viewers, so internal-only tasks never leak.
    """
    q = db.query(Task).filter(
        Task.project_id == project.id, Task.is_archived.is_(False)
    )
    if user.is_external:
        q = q.filter(Task.is_internal_only.is_(False))
    elif not can_view_all_tasks(user):
        q = q.filter(Task.owner_user_id == user.id)
    return q.order_by(
        Task.due_date.is_(None), Task.due_date, Task.updated_at.desc()
    ).all()


def tasks_for_report(
    db: Session, project: Project, user: AppUser, *, include_internal: bool
) -> list[Task]:
    """Non-archived tasks to render in a report of ``project``.

    A report covers the whole POC, so — unlike :func:`visible_project_tasks`,
    which scopes a non-admin user to *their own* tasks in the Task Manager — this
    returns every owner's tasks on the project. ``include_internal`` follows the
    report's audience: a client-facing report excludes internal-only tasks even
    for an internal author, while an internal report includes them. The flag is
    honored only for internal users, so an external viewer never receives
    internal-only tasks regardless of the requested audience.
    """
    q = db.query(Task).filter(
        Task.project_id == project.id, Task.is_archived.is_(False)
    )
    if not (include_internal and user.is_internal):
        q = q.filter(Task.is_internal_only.is_(False))
    return q.order_by(
        Task.due_date.is_(None), Task.due_date, Task.updated_at.desc()
    ).all()


def get_owned_task(db: Session, task_id: int, user: AppUser) -> Task | None:
    """Load a task the user is allowed to see/edit, or None.

    Owners can act on their own tasks; admins can act on any task.
    """
    task = db.get(Task, task_id)
    if task is None:
        return None
    if task.owner_user_id == user.id or can_view_all_tasks(user):
        return task
    return None
