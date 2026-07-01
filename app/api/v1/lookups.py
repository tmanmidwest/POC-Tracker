"""CRUD endpoints for the four lookup tables.

Contact roles, project statuses, feature types, and use-case statuses all share
the same CRUD shape, so they are generated from a small factory. Each carries an
`is_system` flag (seed defaults can't be deleted) and reference checks so a
lookup still in use can't be deleted out from under live data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1._helpers import raise_conflict_if_referenced, raise_conflict_system_row
from app.db import get_db
from app.models import (
    Contact,
    ContactRole,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    Task,
    TaskPriority,
    TaskStatus,
    UseCaseLibrary,
    UseCaseStatus,
)
from app.schemas.lookups import (
    ContactRoleCreate,
    ContactRoleOut,
    ContactRoleUpdate,
    FeatureTypeCreate,
    FeatureTypeOut,
    FeatureTypeUpdate,
    ProjectStatusCreate,
    ProjectStatusOut,
    ProjectStatusUpdate,
    TaskPriorityCreate,
    TaskPriorityOut,
    TaskPriorityUpdate,
    TaskStatusCreate,
    TaskStatusOut,
    TaskStatusUpdate,
    UseCaseStatusCreate,
    UseCaseStatusOut,
    UseCaseStatusUpdate,
)
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal

log = logging.getLogger(__name__)


def _make_lookup_router(
    *,
    prefix: str,
    model: type[Any],
    out_schema: type[BaseModel],
    create_schema: type[BaseModel],
    update_schema: type[BaseModel],
    noun: str,
    event_noun: str,
    order_by: Any,
    references: Callable[[int], list[tuple[str, Any, Any, int]]],
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["lookups"])

    @router.get("/", response_model=list[out_schema])
    def list_rows(
        is_active: bool | None = None,
        db: Session = Depends(get_db),
        _principal: Principal = Depends(get_authenticated_principal),
    ) -> Any:
        query = db.query(model)
        if is_active is not None:
            query = query.filter(model.is_active == is_active)
        return query.order_by(order_by).all()

    @router.get("/{row_id}", response_model=out_schema)
    def get_row(
        row_id: int,
        db: Session = Depends(get_db),
        _principal: Principal = Depends(get_authenticated_principal),
    ) -> Any:
        row = db.get(model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{noun} not found.")
        return row

    @router.post("/", response_model=out_schema, status_code=status.HTTP_201_CREATED)
    def create_row(
        body: create_schema,  # type: ignore[valid-type]
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_authenticated_principal),
    ) -> Any:
        row = model(**body.model_dump())
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A {noun.lower()} with that name already exists.",
            ) from None
        db.refresh(row)
        record_event(
            category="lookup",
            event_type=f"lookup.{event_noun}.created",
            **principal_actor(principal),
            target_type=event_noun,
            target_id=row.id,
            target_label=row.name,
            message=f"Created {noun.lower()} '{row.name}'",
            detail={"surface": "api"},
        )
        return row

    @router.patch("/{row_id}", response_model=out_schema)
    def update_row(
        row_id: int,
        body: update_schema,  # type: ignore[valid-type]
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_authenticated_principal),
    ) -> Any:
        row = db.get(model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{noun} not found.")
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A {noun.lower()} with that name already exists.",
            ) from None
        db.refresh(row)
        record_event(
            category="lookup",
            event_type=f"lookup.{event_noun}.updated",
            **principal_actor(principal),
            target_type=event_noun,
            target_id=row.id,
            target_label=row.name,
            message=f"Updated {noun.lower()} '{row.name}'",
            detail={"surface": "api"},
        )
        return row

    @router.delete("/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_row(
        row_id: int,
        db: Session = Depends(get_db),
        principal: Principal = Depends(get_authenticated_principal),
    ) -> None:
        row = db.get(model, row_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{noun} not found.")
        if getattr(row, "is_system", False):
            raise_conflict_system_row(f"{noun.lower()} '{row.name}'")
        raise_conflict_if_referenced(
            db=db, target_label=f"{noun.lower()} '{row.name}'", references=references(row_id)
        )
        name = row.name
        db.delete(row)
        db.commit()
        record_event(
            category="lookup",
            event_type=f"lookup.{event_noun}.deleted",
            **principal_actor(principal),
            target_type=event_noun,
            target_id=row_id,
            target_label=name,
            message=f"Deleted {noun.lower()} '{name}'",
            detail={"surface": "api"},
        )

    # `from __future__ import annotations` turns the `body: create_schema`
    # parameter annotations into unresolvable string forward refs (they name a
    # closure variable, not a module global). Bind the real schema classes so
    # FastAPI/Pydantic can build request validation and the OpenAPI schema.
    create_row.__annotations__["body"] = create_schema
    update_row.__annotations__["body"] = update_schema

    return router


contact_roles_router = _make_lookup_router(
    prefix="/contact-roles",
    model=ContactRole,
    out_schema=ContactRoleOut,
    create_schema=ContactRoleCreate,
    update_schema=ContactRoleUpdate,
    noun="Contact role",
    event_noun="contact_role",
    order_by=ContactRole.name,
    references=lambda rid: [("contacts", Contact, Contact.role_id, rid)],
)

project_statuses_router = _make_lookup_router(
    prefix="/project-statuses",
    model=ProjectStatus,
    out_schema=ProjectStatusOut,
    create_schema=ProjectStatusCreate,
    update_schema=ProjectStatusUpdate,
    noun="Project status",
    event_noun="project_status",
    order_by=ProjectStatus.sort_order,
    references=lambda rid: [("projects", Project, Project.status_id, rid)],
)

feature_types_router = _make_lookup_router(
    prefix="/feature-types",
    model=FeatureType,
    out_schema=FeatureTypeOut,
    create_schema=FeatureTypeCreate,
    update_schema=FeatureTypeUpdate,
    noun="Feature type",
    event_noun="feature_type",
    order_by=FeatureType.name,
    references=lambda rid: [
        ("library use cases", UseCaseLibrary, UseCaseLibrary.feature_type_id, rid),
        ("project use cases", ProjectUseCase, ProjectUseCase.feature_type_id, rid),
    ],
)

use_case_statuses_router = _make_lookup_router(
    prefix="/use-case-statuses",
    model=UseCaseStatus,
    out_schema=UseCaseStatusOut,
    create_schema=UseCaseStatusCreate,
    update_schema=UseCaseStatusUpdate,
    noun="Use case status",
    event_noun="use_case_status",
    order_by=UseCaseStatus.sort_order,
    references=lambda rid: [
        ("project use cases", ProjectUseCase, ProjectUseCase.status_id, rid)
    ],
)

task_statuses_router = _make_lookup_router(
    prefix="/task-statuses",
    model=TaskStatus,
    out_schema=TaskStatusOut,
    create_schema=TaskStatusCreate,
    update_schema=TaskStatusUpdate,
    noun="Task status",
    event_noun="task_status",
    order_by=TaskStatus.sort_order,
    references=lambda rid: [("tasks", Task, Task.status_id, rid)],
)

task_priorities_router = _make_lookup_router(
    prefix="/task-priorities",
    model=TaskPriority,
    out_schema=TaskPriorityOut,
    create_schema=TaskPriorityCreate,
    update_schema=TaskPriorityUpdate,
    noun="Task priority",
    event_noun="task_priority",
    order_by=TaskPriority.sort_order,
    references=lambda rid: [("tasks", Task, Task.priority_id, rid)],
)
