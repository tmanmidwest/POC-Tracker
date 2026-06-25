"""HTML UI for projects, their use cases, and screenshots."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from itertools import groupby

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    Customer,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    Screenshot,
    UseCaseLibrary,
    UseCaseStatus,
)
from app.models.project_use_case import SOURCE_CUSTOM
from app.services import screenshots as screenshot_store
from app.services.audit import record_event
from app.services.use_cases import (
    added_library_ids,
    copy_library_entries_to_project,
    default_project_status_id,
    default_use_case_status_id,
)
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/projects", tags=["ui"], include_in_schema=False)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _clean_url(value: str | None) -> str | None:
    """Normalize a user-entered URL: trim, default to https://, and reject any
    non-http(s) scheme (e.g. javascript:, data:) so it's safe as a link href."""
    v = _clean(value)
    if v is None:
        return None
    if v.lower().startswith(("http://", "https://")):
        return v
    if _URL_SCHEME_RE.match(v):  # some other scheme — drop it
        return None
    return f"https://{v}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _ref_sort_key(ref: str | None) -> tuple:
    """Sort '1.2' before '1.10' before '2.1'; non-numeric refs sort last."""
    if not ref:
        return (9_999,)
    parts = []
    for chunk in ref.replace("-", ".").split("."):
        chunk = chunk.strip()
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def _grouped_use_cases(project: Project) -> list[dict]:
    """Use cases grouped by category, each list sorted by reference number."""
    ucs = sorted(
        project.use_cases,
        key=lambda u: (u.category.lower(), _ref_sort_key(u.reference_number), u.name.lower()),
    )
    groups = []
    for category, items in groupby(ucs, key=lambda u: u.category):
        groups.append({"category": category, "use_cases": list(items)})
    return groups


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


def _form_dropdowns(db: Session) -> dict:
    return {
        "customers": db.query(Customer).order_by(Customer.name).all(),
        "project_statuses": db.query(ProjectStatus)
        .filter(ProjectStatus.is_active.is_(True))
        .order_by(ProjectStatus.sort_order)
        .all(),
        "users": db.query(AppUser)
        .filter(AppUser.is_active.is_(True))
        .order_by(AppUser.username)
        .all(),
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/")
def list_projects(
    request: Request,
    status_id: int | None = None,
    view: str = "active",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    query = db.query(Project)
    if view == "archived":
        query = query.filter(Project.is_archived.is_(True))
    elif view == "active":
        query = query.filter(Project.is_archived.is_(False))
    if status_id:
        query = query.filter(Project.status_id == status_id)
    projects = query.order_by(Project.updated_at.desc()).all()
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    return render(
        request, "projects/list.html", current_user=user, active_section="projects",
        projects=projects, statuses=statuses, view=view, status_id=status_id,
    )


# ---------------------------------------------------------------------------
# Create / Edit
# ---------------------------------------------------------------------------


@router.get("/new")
def new_form(
    request: Request,
    customer_id: int | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request, "projects/form.html", current_user=user, active_section="projects",
        project=None, form={"customer_id": customer_id, "sales_engineer_id": user.id},
        form_action="/ui/projects/new", **_form_dropdowns(db),
    )


async def _read_project_form(request: Request) -> dict:
    form = await request.form()
    return {
        "customer_id": form.get("customer_id"),
        "name": _clean(form.get("name")),  # type: ignore[arg-type]
        "status_id": form.get("status_id"),
        "start_date": _parse_date(form.get("start_date")),  # type: ignore[arg-type]
        "end_date": _parse_date(form.get("end_date")),  # type: ignore[arg-type]
        "sales_engineer_id": form.get("sales_engineer_id"),
        "account_executive": _clean(form.get("account_executive")),  # type: ignore[arg-type]
        "account_executive_email": _clean(form.get("account_executive_email")),  # type: ignore[arg-type]
        "salesforce_opp_url": _clean_url(form.get("salesforce_opp_url")),  # type: ignore[arg-type]
        "notes": _clean(form.get("notes")),  # type: ignore[arg-type]
    }


@router.post("/new")
async def create_project(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    data = await _read_project_form(request)
    if not data["customer_id"]:
        flash(request, "Customer is required.", "error")
        return render(
            request, "projects/form.html", current_user=user, active_section="projects",
            project=None, form=data, form_action="/ui/projects/new",
            error="Customer is required.", **_form_dropdowns(db),
        )
    status_id = int(data["status_id"]) if data["status_id"] else default_project_status_id(db)
    project = Project(
        customer_id=int(data["customer_id"]),
        name=data["name"],
        status_id=status_id,
        start_date=data["start_date"],
        end_date=data["end_date"],
        sales_engineer_id=int(data["sales_engineer_id"]) if data["sales_engineer_id"] else None,
        account_executive=data["account_executive"],
        account_executive_email=data["account_executive_email"],
        salesforce_opp_url=data["salesforce_opp_url"],
        notes=data["notes"],
    )
    db.add(project)
    db.commit()
    record_event(
        category="project", event_type="project.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Created project '{project.display_name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Project created. Now add use cases.", "success")
    return RedirectResponse(url=f"/ui/projects/{project.id}", status_code=303)


@router.get("/{project_id}/edit")
def edit_form(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    form = {
        "customer_id": project.customer_id,
        "name": project.name,
        "status_id": project.status_id,
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "sales_engineer_id": project.sales_engineer_id,
        "account_executive": project.account_executive,
        "account_executive_email": project.account_executive_email,
        "salesforce_opp_url": project.salesforce_opp_url,
        "notes": project.notes,
    }
    return render(
        request, "projects/form.html", current_user=user, active_section="projects",
        project=project, form=form, form_action=f"/ui/projects/{project_id}/edit",
        **_form_dropdowns(db),
    )


@router.post("/{project_id}/edit")
async def update_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    data = await _read_project_form(request)
    if not data["customer_id"]:
        flash(request, "Customer is required.", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}/edit", status_code=303)
    project.customer_id = int(data["customer_id"])
    project.name = data["name"]
    if data["status_id"]:
        project.status_id = int(data["status_id"])
    project.start_date = data["start_date"]
    project.end_date = data["end_date"]
    project.sales_engineer_id = (
        int(data["sales_engineer_id"]) if data["sales_engineer_id"] else None
    )
    project.account_executive = data["account_executive"]
    project.account_executive_email = data["account_executive_email"]
    project.salesforce_opp_url = data["salesforce_opp_url"]
    project.notes = data["notes"]
    db.commit()
    record_event(
        category="project", event_type="project.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Updated project '{project.display_name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Project updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}", status_code=303)


@router.post("/{project_id}/archive")
def archive_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    project.is_archived = True
    project.archived_at = datetime.now(UTC)
    db.commit()
    flash(request, "Project archived.", "success")
    return RedirectResponse(url="/ui/projects", status_code=303)


@router.post("/{project_id}/restore")
def restore_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    project.is_archived = False
    project.archived_at = None
    db.commit()
    flash(request, "Project restored.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}", status_code=303)


@router.post("/{project_id}/delete")
def delete_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    label = project.display_name
    for uc in project.use_cases:
        for shot in uc.screenshots:
            screenshot_store.delete_file(shot)
    db.delete(project)
    db.commit()
    record_event(
        category="project", event_type="project.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project_id, target_label=label,
        message=f"Deleted project '{label}'", detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Project '{label}' deleted.", "success")
    return RedirectResponse(url="/ui/projects", status_code=303)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get("/{project_id}")
def detail(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)

    # Library picker — entries grouped by category with an "already added" flag.
    added = added_library_ids(project)
    library = (
        db.query(UseCaseLibrary)
        .filter(UseCaseLibrary.is_active.is_(True))
        .order_by(UseCaseLibrary.category, UseCaseLibrary.default_reference_number)
        .all()
    )
    lib_groups = []
    for category, items in groupby(library, key=lambda e: e.category):
        entries = [{"entry": e, "added": e.id in added} for e in items]
        lib_groups.append({"category": category, "entries": entries})

    uc_statuses = (
        db.query(UseCaseStatus)
        .filter(UseCaseStatus.is_active.is_(True))
        .order_by(UseCaseStatus.sort_order)
        .all()
    )
    feature_types = (
        db.query(FeatureType)
        .filter(FeatureType.is_active.is_(True))
        .order_by(FeatureType.name)
        .all()
    )

    total = len(project.use_cases)
    done = sum(1 for uc in project.use_cases if uc.status and uc.status.is_complete_status)

    return render(
        request, "projects/detail.html", current_user=user, active_section="projects",
        project=project, use_case_groups=_grouped_use_cases(project),
        library_groups=lib_groups, uc_statuses=uc_statuses, feature_types=feature_types,
        progress={"total": total, "done": done, "pct": round(done / total * 100) if total else 0},
    )


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


@router.post("/{project_id}/use-cases/from-library")
async def add_from_library(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    form = await request.form()
    raw_ids = form.getlist("library_ids")  # type: ignore[attr-defined]
    library_ids = [int(x) for x in raw_ids if str(x).isdigit()]
    created = copy_library_entries_to_project(db, project, library_ids)
    db.commit()
    record_event(
        category="project", event_type="use_case.added_from_library", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Added {len(created)} use case(s) from library",
        detail={"surface": "ui", "count": len(created)}, request=request,
    )
    flash(request, f"Added {len(created)} use case(s) from the library.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


@router.post("/{project_id}/use-cases")
def add_custom_use_case(
    project_id: int,
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    reference_number: str | None = Form(None),
    description: str | None = Form(None),
    success_validation: str | None = Form(None),
    feature_type_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    if not _clean(category) or not _clean(name):
        flash(request, "Category and name are required for a use case.", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)
    uc = ProjectUseCase(
        project_id=project.id,
        source=SOURCE_CUSTOM,
        reference_number=_clean(reference_number),
        category=_clean(category),
        name=_clean(name),
        description=_clean(description),
        success_validation=_clean(success_validation),
        feature_type_id=int(feature_type_id) if feature_type_id else None,
        status_id=default_use_case_status_id(db),
    )
    db.add(uc)
    db.commit()
    record_event(
        category="project", event_type="use_case.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_use_case",
        target_id=uc.id, target_label=uc.name,
        message=f"Added custom use case '{uc.name}'",
        detail={"surface": "ui", "project_id": project_id}, request=request,
    )
    flash(request, f"Added use case '{uc.name}'.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


@router.post("/use-cases/{use_case_id}/edit")
def update_use_case(
    use_case_id: int,
    request: Request,
    reference_number: str | None = Form(None),
    category: str = Form(...),
    name: str = Form(...),
    description: str | None = Form(None),
    success_validation: str | None = Form(None),
    feature_type_id: str | None = Form(None),
    status_id: str | None = Form(None),
    comments: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    uc = _get_use_case(db, use_case_id)
    uc.reference_number = _clean(reference_number)
    uc.category = _clean(category) or uc.category
    uc.name = _clean(name) or uc.name
    uc.description = _clean(description)
    uc.success_validation = _clean(success_validation)
    uc.feature_type_id = int(feature_type_id) if feature_type_id else None
    if status_id:
        uc.status_id = int(status_id)
    uc.comments = _clean(comments)
    db.commit()
    record_event(
        category="project", event_type="use_case.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_use_case",
        target_id=uc.id, target_label=uc.name,
        message=f"Updated use case '{uc.name}'", detail={"surface": "ui"}, request=request,
    )
    flash(request, "Use case updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)


@router.post("/use-cases/{use_case_id}/status")
def quick_set_status(
    use_case_id: int,
    request: Request,
    status_id: int = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Inline status change from the use-case list."""
    uc = _get_use_case(db, use_case_id)
    uc.status_id = status_id
    db.commit()
    record_event(
        category="project", event_type="use_case.status_changed", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_use_case",
        target_id=uc.id, target_label=uc.name,
        message=f"Set use case '{uc.name}' status",
        detail={"surface": "ui", "status_id": status_id}, request=request,
    )
    return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)


@router.post("/use-cases/{use_case_id}/delete")
def delete_use_case(
    use_case_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    uc = _get_use_case(db, use_case_id)
    project_id = uc.project_id
    name = uc.name
    for shot in uc.screenshots:
        screenshot_store.delete_file(shot)
    db.delete(uc)
    db.commit()
    record_event(
        category="project", event_type="use_case.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_use_case",
        target_id=use_case_id, target_label=name,
        message=f"Deleted use case '{name}'", detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Use case '{name}' removed.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


@router.post("/use-cases/{use_case_id}/screenshots")
async def upload_screenshot(
    use_case_id: int,
    request: Request,
    file: UploadFile = File(...),
    caption: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    uc = _get_use_case(db, use_case_id)
    content = await file.read()
    if not content:
        flash(request, "No file was uploaded.", "error")
        return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)
    if file.content_type not in screenshot_store.ALLOWED_CONTENT_TYPES:
        flash(request, "Only PNG, JPEG, GIF, or WebP images are supported.", "error")
        return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)
    if len(content) > screenshot_store.MAX_SIZE_BYTES:
        flash(request, "Image is too large (10 MB max).", "error")
        return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)

    stored = screenshot_store.store_bytes(content, file.content_type)
    shot = Screenshot(
        project_use_case_id=uc.id,
        stored_filename=stored,
        original_filename=file.filename,
        content_type=file.content_type,
        size_bytes=len(content),
        caption=_clean(caption),
    )
    db.add(shot)
    db.commit()
    record_event(
        category="project", event_type="screenshot.uploaded", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_use_case",
        target_id=uc.id, target_label=uc.name,
        message=f"Uploaded screenshot to '{uc.name}'",
        detail={"surface": "ui", "filename": file.filename}, request=request,
    )
    flash(request, "Screenshot uploaded.", "success")
    return RedirectResponse(url=f"/ui/projects/{uc.project_id}#use-cases", status_code=303)


@router.get("/screenshots/{shot_id}")
def serve_screenshot(
    shot_id: int,
    db: Session = Depends(get_db),
    _user: AppUser = Depends(require_ui_user),
) -> Response:
    shot = db.get(Screenshot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    path = screenshot_store.path_for(shot)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file missing.")
    # Serve inline so clicking a thumbnail opens the image in the browser
    # instead of downloading it. Passing `filename=` would make Starlette set
    # Content-Disposition: attachment, which forces a download.
    download_name = shot.original_filename or shot.stored_filename
    return FileResponse(
        path,
        media_type=shot.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{download_name}"'},
    )


@router.post("/screenshots/{shot_id}/delete")
def delete_screenshot(
    shot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    shot = db.get(Screenshot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    project_id = shot.use_case.project_id
    screenshot_store.delete_file(shot)
    db.delete(shot)
    db.commit()
    flash(request, "Screenshot deleted.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)
