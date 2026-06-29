"""Library set (named use-case library) CRUD endpoints.

A library set groups use-case library entries. Deleting a set is blocked while
it still contains entries — move or delete those first so project snapshots and
provenance stay coherent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LibrarySet
from app.schemas.poc import LibrarySetCreate, LibrarySetOut, LibrarySetUpdate
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal
from app.services.library_sets import entry_count, list_library_sets

log = logging.getLogger(__name__)

router = APIRouter(prefix="/library-sets", tags=["library-sets"])


def _check_name_unique(db: Session, name: str, exclude_id: int | None = None) -> None:
    existing = db.query(LibrarySet).filter(LibrarySet.name == name).first()
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(status_code=409, detail="A library with that name already exists.")


@router.get("/", response_model=list[LibrarySetOut])
def list_sets(
    is_active: bool | None = None,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[LibrarySet]:
    sets = list_library_sets(db, include_inactive=True)
    if is_active is not None:
        sets = [s for s in sets if s.is_active == is_active]
    return sets


@router.get("/{set_id}", response_model=LibrarySetOut)
def get_set(
    set_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> LibrarySet:
    entry = db.get(LibrarySet, set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library not found.")
    return entry


@router.post("/", response_model=LibrarySetOut, status_code=status.HTTP_201_CREATED)
def create_set(
    body: LibrarySetCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> LibrarySet:
    _check_name_unique(db, body.name)
    entry = LibrarySet(**body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    record_event(
        category="library",
        event_type="library.set.created",
        **principal_actor(principal),
        target_type="library_set",
        target_id=entry.id,
        target_label=entry.name,
        message=f"Created library '{entry.name}'",
        detail={"surface": "api"},
    )
    return entry


@router.patch("/{set_id}", response_model=LibrarySetOut)
def update_set(
    set_id: int,
    body: LibrarySetUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> LibrarySet:
    entry = db.get(LibrarySet, set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library not found.")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        _check_name_unique(db, data["name"], exclude_id=set_id)
    for field, value in data.items():
        setattr(entry, field, value)
    db.commit()
    db.refresh(entry)
    record_event(
        category="library",
        event_type="library.set.updated",
        **principal_actor(principal),
        target_type="library_set",
        target_id=entry.id,
        target_label=entry.name,
        message=f"Updated library '{entry.name}'",
        detail={"surface": "api"},
    )
    return entry


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_set(
    set_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    entry = db.get(LibrarySet, set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library not found.")
    if entry.is_default:
        raise HTTPException(
            status_code=409, detail="The default library can't be deleted."
        )
    count = entry_count(db, set_id)
    if count:
        raise HTTPException(
            status_code=409,
            detail=f"This library still has {count} use case(s). "
            "Move or delete them before deleting the library.",
        )
    name = entry.name
    db.delete(entry)
    db.commit()
    record_event(
        category="library",
        event_type="library.set.deleted",
        **principal_actor(principal),
        target_type="library_set",
        target_id=set_id,
        target_label=name,
        message=f"Deleted library '{name}'",
        detail={"surface": "api"},
    )
