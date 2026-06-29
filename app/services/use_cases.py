"""Shared helpers for project use cases — snapshot/copy logic and defaults.

Used by both the REST API and the web UI so the "copy a library entry into a
project" behaviour is identical everywhere.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, ProjectStatus, ProjectUseCase, UseCaseLibrary, UseCaseStatus
from app.models.project_use_case import SOURCE_LIBRARY


def default_use_case_status_id(db: Session) -> int | None:
    """The lowest-sort active use-case status (e.g. 'Pending Testing')."""
    row = db.scalar(
        select(UseCaseStatus)
        .where(UseCaseStatus.is_active.is_(True))
        .order_by(UseCaseStatus.sort_order)
        .limit(1)
    )
    return row.id if row else None


def default_project_status_id(db: Session) -> int | None:
    """The lowest-sort active project status (e.g. 'Pending Scheduling')."""
    row = db.scalar(
        select(ProjectStatus)
        .where(ProjectStatus.is_active.is_(True))
        .order_by(ProjectStatus.sort_order)
        .limit(1)
    )
    return row.id if row else None


def added_library_ids(project: Project) -> set[int]:
    """Library ids already pulled into this project (for picker de-dup)."""
    return {uc.library_id for uc in project.use_cases if uc.library_id is not None}


def copy_library_entries_to_project(
    db: Session, project: Project, library_ids: list[int]
) -> list[ProjectUseCase]:
    """Copy the given library entries into the project as snapshots.

    Skips any library entry already present in the project (de-dup), so calling
    this again after re-opening the picker never creates duplicates. Caller is
    responsible for committing.
    """
    status_id = default_use_case_status_id(db)
    already = added_library_ids(project)
    created: list[ProjectUseCase] = []
    for lib_id in library_ids:
        if lib_id in already:
            continue
        lib = db.get(UseCaseLibrary, lib_id)
        if lib is None:
            continue
        uc = ProjectUseCase(
            project_id=project.id,
            source=SOURCE_LIBRARY,
            library_id=lib.id,
            library_set_id=lib.library_set_id,
            reference_number=lib.default_reference_number,
            category=lib.category,
            name=lib.name,
            description=lib.description,
            success_validation=lib.success_validation,
            feature_type_id=lib.feature_type_id,
            status_id=status_id,
        )
        db.add(uc)
        created.append(uc)
        already.add(lib_id)
    return created
