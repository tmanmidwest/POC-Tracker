"""Project CRUD endpoints, plus nested project use cases.

Standard users and admins can both manage projects and their use cases (shared
edit model). Authentication is via API key or OAuth bearer token.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    Customer,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    UseCaseStatus,
)
from app.models.project_use_case import SOURCE_CUSTOM
from app.schemas.poc import (
    AddLibraryUseCases,
    ProjectCreate,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdate,
    ProjectUseCaseCreate,
    ProjectUseCaseOut,
    ProjectUseCaseUpdate,
)
from app.services import screenshots as screenshot_store
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal
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
