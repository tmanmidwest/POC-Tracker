"""Project CRUD endpoints, plus nested project use cases.

Standard users and admins can both manage projects and their use cases (shared
edit model). Authentication is via API key or OAuth bearer token.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    Customer,
    FeatureType,
    Project,
    ProjectNote,
    ProjectStatus,
    ProjectType,
    ProjectUseCase,
    UseCaseStatus,
)
from app.models.project_use_case import SOURCE_CUSTOM
from app.schemas.poc import (
    AddLibraryUseCases,
    ProjectCreate,
    ProjectDetailOut,
    ProjectNoteCreate,
    ProjectNoteOut,
    ProjectNoteUpdate,
    ProjectOut,
    ProjectUpdate,
    ProjectUseCaseCreate,
    ProjectUseCaseOut,
    ProjectUseCaseUpdate,
)
from app.services import note_attachments as note_store
from app.services import screenshots as screenshot_store
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal
from app.services.rich_text import html_to_text, sanitize_note_html
from app.services.use_cases import (
    copy_library_entries_to_project,
    default_project_status_id,
    default_use_case_status_id,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _get_use_case(db: Session, use_case_id: int) -> ProjectUseCase:
    uc = db.get(ProjectUseCase, use_case_id)
    if uc is None:
        raise HTTPException(status_code=404, detail="Use case not found.")
    return uc


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[ProjectOut])
def list_projects(
    status_id: int | None = None,
    customer_id: int | None = None,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[Project]:
    query = db.query(Project)
    if not include_archived:
        query = query.filter(Project.is_archived.is_(False))
    if status_id is not None:
        query = query.filter(Project.status_id == status_id)
    if customer_id is not None:
        query = query.filter(Project.customer_id == customer_id)
    return query.order_by(Project.id.desc()).all()


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> Project:
    return _get_project(db, project_id)


@router.post("/", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Project:
    if db.get(Customer, body.customer_id) is None:
        raise HTTPException(status_code=422, detail="Unknown customer.")
    data = body.model_dump()
    status_id = data.get("status_id") or default_project_status_id(db)
    if status_id is None or db.get(ProjectStatus, status_id) is None:
        raise HTTPException(status_code=422, detail="A valid project status is required.")
    data["status_id"] = status_id
    _validate_project_fks(db, data)
    project = Project(**data)
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Database error: {exc.orig}") from None
    db.refresh(project)
    record_event(
        category="project",
        event_type="project.created",
        **principal_actor(principal),
        target_type="project",
        target_id=project.id,
        target_label=project.display_name,
        message=f"Created project '{project.display_name}'",
        detail={"surface": "api"},
    )
    return project


def _validate_project_fks(db: Session, data: dict) -> None:
    se_id = data.get("sales_engineer_id")
    if se_id is not None and db.get(AppUser, se_id) is None:
        raise HTTPException(status_code=422, detail="Unknown sales engineer (app user).")
    st_id = data.get("status_id")
    if st_id is not None and db.get(ProjectStatus, st_id) is None:
        raise HTTPException(status_code=422, detail="Unknown project status.")
    type_id = data.get("type_id")
    if type_id is not None and db.get(ProjectType, type_id) is None:
        raise HTTPException(status_code=422, detail="Unknown project type.")
    cust_id = data.get("customer_id")
    if cust_id is not None and db.get(Customer, cust_id) is None:
        raise HTTPException(status_code=422, detail="Unknown customer.")


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Project:
    project = _get_project(db, project_id)
    data = body.model_dump(exclude_unset=True)
    _validate_project_fks(db, data)
    if data.get("is_archived") is True and not project.is_archived:
        project.archived_at = datetime.now(UTC)
    elif data.get("is_archived") is False:
        project.archived_at = None
    for field, value in data.items():
        setattr(project, field, value)
    db.commit()
    db.refresh(project)
    record_event(
        category="project",
        event_type="project.updated",
        **principal_actor(principal),
        target_type="project",
        target_id=project.id,
        target_label=project.display_name,
        message=f"Updated project '{project.display_name}'",
        detail={"surface": "api", "fields": list(data.keys())},
    )
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    project = _get_project(db, project_id)
    label = project.display_name
    # Remove screenshot files before the DB cascade drops their rows.
    for uc in project.use_cases:
        for shot in uc.screenshots:
            screenshot_store.delete_file(shot)
    db.delete(project)
    db.commit()
    record_event(
        category="project",
        event_type="project.deleted",
        **principal_actor(principal),
        target_type="project",
        target_id=project_id,
        target_label=label,
        message=f"Deleted project '{label}'",
        detail={"surface": "api"},
    )


# ---------------------------------------------------------------------------
# Project use cases
# ---------------------------------------------------------------------------


@router.get("/{project_id}/use-cases", response_model=list[ProjectUseCaseOut])
def list_use_cases(
    project_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[ProjectUseCase]:
    _get_project(db, project_id)
    return (
        db.query(ProjectUseCase)
        .filter(ProjectUseCase.project_id == project_id)
        .order_by(ProjectUseCase.category, ProjectUseCase.reference_number)
        .all()
    )


@router.post(
    "/{project_id}/use-cases",
    response_model=ProjectUseCaseOut,
    status_code=status.HTTP_201_CREATED,
)
def add_custom_use_case(
    project_id: int,
    body: ProjectUseCaseCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> ProjectUseCase:
    """Add an ad-hoc (customer-provided) use case not sourced from the library."""
    _get_project(db, project_id)
    data = body.model_dump()
    status_id = data.get("status_id") or default_use_case_status_id(db)
    if status_id is None or db.get(UseCaseStatus, status_id) is None:
        raise HTTPException(status_code=422, detail="A valid use-case status is required.")
    if data.get("feature_type_id") and db.get(FeatureType, data["feature_type_id"]) is None:
        raise HTTPException(status_code=422, detail="Unknown feature type.")
    data["status_id"] = status_id
    uc = ProjectUseCase(project_id=project_id, source=SOURCE_CUSTOM, **data)
    db.add(uc)
    db.commit()
    db.refresh(uc)
    record_event(
        category="project",
        event_type="use_case.created",
        **principal_actor(principal),
        target_type="project_use_case",
        target_id=uc.id,
        target_label=uc.name,
        message=f"Added custom use case '{uc.name}'",
        detail={"surface": "api", "project_id": project_id},
    )
    return uc


@router.post(
    "/{project_id}/use-cases/from-library",
    response_model=list[ProjectUseCaseOut],
    status_code=status.HTTP_201_CREATED,
)
def add_library_use_cases(
    project_id: int,
    body: AddLibraryUseCases,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> list[ProjectUseCase]:
    """Copy library entries into the project as snapshots (de-duplicated)."""
    project = _get_project(db, project_id)
    created = copy_library_entries_to_project(db, project, body.library_ids)
    db.commit()
    for uc in created:
        db.refresh(uc)
    record_event(
        category="project",
        event_type="use_case.added_from_library",
        **principal_actor(principal),
        target_type="project",
        target_id=project_id,
        target_label=project.display_name,
        message=f"Added {len(created)} use case(s) from library",
        detail={"surface": "api", "count": len(created)},
    )
    return created


@router.patch("/use-cases/{use_case_id}", response_model=ProjectUseCaseOut)
def update_use_case(
    use_case_id: int,
    body: ProjectUseCaseUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> ProjectUseCase:
    uc = _get_use_case(db, use_case_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("status_id") and db.get(UseCaseStatus, data["status_id"]) is None:
        raise HTTPException(status_code=422, detail="Unknown use-case status.")
    if data.get("feature_type_id") and db.get(FeatureType, data["feature_type_id"]) is None:
        raise HTTPException(status_code=422, detail="Unknown feature type.")
    for field, value in data.items():
        setattr(uc, field, value)
    db.commit()
    db.refresh(uc)
    record_event(
        category="project",
        event_type="use_case.updated",
        **principal_actor(principal),
        target_type="project_use_case",
        target_id=uc.id,
        target_label=uc.name,
        message=f"Updated use case '{uc.name}'",
        detail={"surface": "api", "fields": list(data.keys())},
    )
    return uc


@router.delete("/use-cases/{use_case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_use_case(
    use_case_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    uc = _get_use_case(db, use_case_id)
    name = uc.name
    for shot in uc.screenshots:
        screenshot_store.delete_file(shot)
    db.delete(uc)
    db.commit()
    record_event(
        category="project",
        event_type="use_case.deleted",
        **principal_actor(principal),
        target_type="project_use_case",
        target_id=use_case_id,
        target_label=name,
        message=f"Deleted use case '{name}'",
        detail={"surface": "api"},
    )


# ---------------------------------------------------------------------------
# Project notes (dated journal entries)
# ---------------------------------------------------------------------------


def _get_note(db: Session, note_id: int) -> ProjectNote:
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    return note


@router.get("/{project_id}/notes", response_model=list[ProjectNoteOut])
def list_notes(
    project_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[ProjectNote]:
    """List a project's journal notes, newest first."""
    _get_project(db, project_id)
    return (
        db.query(ProjectNote)
        .filter(ProjectNote.project_id == project_id)
        .order_by(ProjectNote.note_date.desc(), ProjectNote.id.desc())
        .all()
    )


@router.post(
    "/{project_id}/notes",
    response_model=ProjectNoteOut,
    status_code=status.HTTP_201_CREATED,
)
def add_note(
    project_id: int,
    body: ProjectNoteCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> ProjectNote:
    """Add a dated journal note to a project. ``body`` is sanitized HTML."""
    project = _get_project(db, project_id)
    body_html = sanitize_note_html(body.body)
    text = html_to_text(body_html)
    if not text:
        raise HTTPException(status_code=422, detail="Note text is required.")
    note = ProjectNote(
        project_id=project_id,
        note_date=body.note_date or date.today(),
        body=text,
        body_html=body_html,
        created_by=body.created_by or principal.identifier,
        is_internal_only=body.is_internal_only,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    record_event(
        category="project",
        event_type="note.added",
        **principal_actor(principal),
        target_type="project",
        target_id=project_id,
        target_label=project.display_name,
        message=f"Added a note to '{project.display_name}'",
        detail={"surface": "api", "note_id": note.id},
    )
    return note


@router.get("/notes/{note_id}", response_model=ProjectNoteOut)
def get_note(
    note_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> ProjectNote:
    return _get_note(db, note_id)


@router.patch("/notes/{note_id}", response_model=ProjectNoteOut)
def update_note(
    note_id: int,
    body: ProjectNoteUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> ProjectNote:
    note = _get_note(db, note_id)
    data = body.model_dump(exclude_unset=True)
    if "body" in data and data["body"] is not None:
        body_html = sanitize_note_html(data["body"])
        text = html_to_text(body_html)
        if not text:
            raise HTTPException(status_code=422, detail="Note text is required.")
        note.body_html = body_html
        note.body = text
    if data.get("note_date") is not None:
        note.note_date = data["note_date"]
    if data.get("is_internal_only") is not None:
        note.is_internal_only = data["is_internal_only"]
    db.commit()
    db.refresh(note)
    record_event(
        category="project",
        event_type="note.updated",
        **principal_actor(principal),
        target_type="project",
        target_id=note.project_id,
        target_label=note.project.display_name,
        message=f"Updated a note on '{note.project.display_name}'",
        detail={"surface": "api", "note_id": note.id, "fields": list(data.keys())},
    )
    return note


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    note = _get_note(db, note_id)
    project_id = note.project_id
    label = note.project.display_name
    # Remove attachment files before the DB cascade drops their rows.
    for att in note.attachments:
        note_store.delete_file(att)
    db.delete(note)
    db.commit()
    record_event(
        category="project",
        event_type="note.deleted",
        **principal_actor(principal),
        target_type="project",
        target_id=project_id,
        target_label=label,
        message=f"Deleted a note from '{label}'",
        detail={"surface": "api", "note_id": note_id},
    )
