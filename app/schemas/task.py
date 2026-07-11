"""Pydantic schemas for the Task Manager REST API.

Tasks are per-user owned. Because the REST API authenticates as a machine
identity (an admin-issued API key or OAuth client), not a logged-in user, the
create/update payloads take an explicit ``owner`` (a username or user id) so the
caller says whose task it is. Statuses and priorities accept a name or id.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.poc import NamedRef, UserRef


class TaskProjectRef(BaseModel):
    """The project a task is assigned to, if any."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str | None
    # Project.display_name falls back to the customer name when name is unset.
    display_name: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    owner: UserRef | None
    title: str
    status_id: int
    status: NamedRef
    priority_id: int | None
    priority: NamedRef | None
    project_id: int | None
    project: TaskProjectRef | None
    start_date: date | None
    due_date: date | None
    details: str | None
    details_html: str | None
    is_archived: bool
    is_internal_only: bool
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    """Create a task. ``owner`` (username or id) and ``title`` are required.

    ``status`` / ``priority`` accept a name or id (status defaults to the first
    active status). ``details`` may contain limited HTML; it is sanitized and a
    plain-text rendering is stored alongside it. ``is_internal_only`` hides the
    task from external viewers (default false).
    """

    owner: str | int
    title: str = Field(min_length=1, max_length=300)
    status: str | int | None = None
    priority: str | int | None = None
    project_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    details: str | None = None
    is_internal_only: bool = False


class TaskUpdate(BaseModel):
    """Update a task. Only provided fields change. ``owner`` reassigns ownership."""

    owner: str | int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: str | int | None = None
    priority: str | int | None = None
    project_id: int | None = None
    start_date: date | None = None
    due_date: date | None = None
    details: str | None = None
    is_archived: bool | None = None
    is_internal_only: bool | None = None
