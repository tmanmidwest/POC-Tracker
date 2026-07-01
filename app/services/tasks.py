"""Helpers for the per-user Task Manager.

Tasks are owned by the user who created them. These helpers centralize the
ownership rule used by every task UI surface: a user sees their own tasks;
admins may additionally view everyone's. External viewers never have task
access (their routes are gated out at the router level).
"""

from __future__ import annotations

from sqlalchemy.orm import Query, Session

from app.models import AppUser, Task

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
