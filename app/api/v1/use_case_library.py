"""Use-case library (master template list) CRUD endpoints.

The library is only a source to pick from. Deleting a library entry does NOT
affect project use cases already copied from it — those are independent
snapshots (their library_id is set to NULL on delete).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import FeatureType, UseCaseLibrary
from app.schemas.poc import (
    UseCaseLibraryCreate,
    UseCaseLibraryOut,
    UseCaseLibraryUpdate,
)
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/use-case-library", tags=["use-case-library"])


def _validate_feature_type(db: Session, feature_type_id: int | None) -> None:
    if feature_type_id is not None and db.get(FeatureType, feature_type_id) is None:
        raise HTTPException(status_code=422, detail="Unknown feature type.")


@router.get("/", response_model=list[UseCaseLibraryOut])
def list_library(
    is_active: bool | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[UseCaseLibrary]:
    query = db.query(UseCaseLibrary)
    if is_active is not None:
        query = query.filter(UseCaseLibrary.is_active == is_active)
    if category is not None:
        query = query.filter(UseCaseLibrary.category == category)
    return query.order_by(
        UseCaseLibrary.category, UseCaseLibrary.default_reference_number, UseCaseLibrary.name
    ).all()


@router.get("/{entry_id}", response_model=UseCaseLibraryOut)
def get_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> UseCaseLibrary:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    return entry


@router.post("/", response_model=UseCaseLibraryOut, status_code=status.HTTP_201_CREATED)
def create_entry(
    body: UseCaseLibraryCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> UseCaseLibrary:
    _validate_feature_type(db, body.feature_type_id)
    entry = UseCaseLibrary(**body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    record_event(
        category="library",
        event_type="library.use_case.created",
        **principal_actor(principal),
        target_type="use_case_library",
        target_id=entry.id,
        target_label=entry.name,
        message=f"Created library use case '{entry.name}'",
        detail={"surface": "api", "category": entry.category},
    )
    return entry


@router.patch("/{entry_id}", response_model=UseCaseLibraryOut)
def update_entry(
    entry_id: int,
    body: UseCaseLibraryUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> UseCaseLibrary:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    data = body.model_dump(exclude_unset=True)
    if "feature_type_id" in data:
        _validate_feature_type(db, data["feature_type_id"])
    for field, value in data.items():
        setattr(entry, field, value)
    db.commit()
    db.refresh(entry)
    record_event(
        category="library",
        event_type="library.use_case.updated",
        **principal_actor(principal),
        target_type="use_case_library",
        target_id=entry.id,
        target_label=entry.name,
        message=f"Updated library use case '{entry.name}'",
        detail={"surface": "api"},
    )
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    name = entry.name
    # Project use cases keep their snapshot; their library_id is set NULL by the FK.
    db.delete(entry)
    db.commit()
    record_event(
        category="library",
        event_type="library.use_case.deleted",
        **principal_actor(principal),
        target_type="use_case_library",
        target_id=entry_id,
        target_label=name,
        message=f"Deleted library use case '{name}'",
        detail={"surface": "api"},
    )
