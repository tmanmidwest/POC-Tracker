"""Pydantic schemas for lookup-table CRUD endpoints.

Naming convention:
- `*Out`     — what the API returns
- `*Create`  — what the client sends to POST
- `*Update`  — what the client sends to PATCH (all fields optional)
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Contact Role
# ---------------------------------------------------------------------------


class ContactRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class ContactRoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_active: bool = True


class ContactRoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Project Status
# ---------------------------------------------------------------------------


class ProjectStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int
    is_terminal: bool
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class ProjectStatusCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 100
    is_terminal: bool = False
    is_active: bool = True


class ProjectStatusUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    is_terminal: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Feature Type
# ---------------------------------------------------------------------------


class FeatureTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class FeatureTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class FeatureTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Project Type
# ---------------------------------------------------------------------------


class ProjectTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class ProjectTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class ProjectTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Use Case Status
# ---------------------------------------------------------------------------


class UseCaseStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int
    is_complete_status: bool
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class UseCaseStatusCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 100
    is_complete_status: bool = False
    is_active: bool = True


class UseCaseStatusUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    is_complete_status: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Task Status
# ---------------------------------------------------------------------------


class TaskStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int
    is_terminal: bool
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class TaskStatusCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 100
    is_terminal: bool = False
    is_active: bool = True


class TaskStatusUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    is_terminal: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Task Priority
# ---------------------------------------------------------------------------


class TaskPriorityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort_order: int
    color: str | None
    is_active: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


class TaskPriorityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sort_order: int = 100
    color: str | None = Field(default=None, max_length=20)
    is_active: bool = True


class TaskPriorityUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    color: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None
