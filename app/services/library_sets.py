"""Shared helpers for named library sets.

Keeps the "which library are we looking at" logic identical across the REST API
and the web UI: list them, find a sensible default, and count entries (used to
guard deletion of a non-empty library).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LibrarySet, UseCaseLibrary


def list_library_sets(db: Session, *, include_inactive: bool = True) -> list[LibrarySet]:
    """All library sets ordered by name (active first when listing inactive too)."""
    query = select(LibrarySet)
    if not include_inactive:
        query = query.where(LibrarySet.is_active.is_(True))
    return list(
        db.scalars(query.order_by(LibrarySet.is_active.desc(), LibrarySet.name)).all()
    )


def default_library_set(db: Session) -> LibrarySet | None:
    """The library to fall back to when none is selected (first active by name)."""
    return db.scalar(
        select(LibrarySet)
        .where(LibrarySet.is_active.is_(True))
        .order_by(LibrarySet.name)
        .limit(1)
    ) or db.scalar(select(LibrarySet).order_by(LibrarySet.name).limit(1))


def resolve_library_set(db: Session, set_id: int | None) -> LibrarySet | None:
    """Return the requested set if it exists, otherwise the default."""
    if set_id is not None:
        found = db.get(LibrarySet, set_id)
        if found is not None:
            return found
    return default_library_set(db)


def entry_count(db: Session, set_id: int) -> int:
    """How many library entries live in a set (for delete guards / display)."""
    return (
        db.scalar(
            select(func.count())
            .select_from(UseCaseLibrary)
            .where(UseCaseLibrary.library_set_id == set_id)
        )
        or 0
    )
