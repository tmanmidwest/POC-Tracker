"""HTML UI for projects, their use cases, and screenshots."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, date, datetime
from itertools import groupby

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    Customer,
    FeatureType,
    NoteAttachment,
    Project,
    ProjectGrant,
    ProjectNote,
    ProjectStatus,
    ProjectUseCase,
    Screenshot,
    UseCaseLibrary,
    UseCaseStatus,
    UseCaseViewPref,
)
from app.models.project_use_case import SOURCE_CUSTOM
from app.services import note_attachments as note_store
from app.services import screenshots as screenshot_store
from app.services.access import (
    accessible_project_ids,
    can_grant_project,
    can_view_project,
)
from app.services.ai.base import GenerationError
from app.services.ai.extraction import extract_use_cases
from app.services.ai.registry import get_provider_spec
from app.services.ai.summaries import (
    default_provider,
    generate_project_summary,
    stream_project_summary,
)
from app.services.audit import record_event
from app.services.rich_text import html_to_text, sanitize_note_html
from app.services.text_extract import TextExtractError, extract_text
from app.services.use_case_io import (
    SpreadsheetError,
    build_export_xlsx,
    build_template_xlsx,
    classify_rows,
    parse_spreadsheet,
)
from app.services.use_cases import (
    added_library_ids,
    copy_library_entries_to_project,
    default_project_status_id,
    default_use_case_status_id,
)
from app.ui.dependencies import require_internal_ui, require_ui_user
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


# Files the AI providers can read natively (no pre-flattening). ~20 MB cap keeps
# the base64 request body within provider limits; larger files fall back to text.
_MAX_NATIVE_BYTES = 20 * 1024 * 1024
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _is_native_doc(filename: str, content_type: str | None) -> bool:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    return (
        name.endswith(".pdf")
        or ctype == "application/pdf"
        or name.endswith(_IMAGE_EXTS)
        or ctype.startswith("image/")
    )


def _native_media_type(filename: str, content_type: str | None) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or ctype == "application/pdf":
        return "application/pdf"
    if ctype.startswith("image/"):
        return ctype
    # Infer an image type from the extension when the browser didn't send one.
    if name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    return "application/pdf"


def _ref_sort_key(ref: str | None) -> tuple:
    """Sort '1.2' before '1.10' before '2.1'; non-numeric refs sort last."""
    if not ref:
        return (9_999,)
    parts = []
    for chunk in ref.replace("-", ".").split("."):
        chunk = chunk.strip()
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def _group_use_cases(use_cases: list[ProjectUseCase]) -> list[dict]:
    """Group the given use cases by category, each list sorted by reference number."""
    ucs = sorted(
        use_cases,
        key=lambda u: (u.category.lower(), _ref_sort_key(u.reference_number), u.name.lower()),
    )
    groups = []
    for category, items in groupby(ucs, key=lambda u: u.category):
        groups.append({"category": category, "use_cases": list(items)})
    return groups


def _grouped_use_cases(project: Project) -> list[dict]:
    """All of a project's use cases, grouped by category."""
    return _group_use_cases(list(project.use_cases))


# Toggleable use-case fields on the project page. "name" and "status" always show.
ALL_UC_FIELDS = [
    {"key": "ref", "label": "Ref #"},
    {"key": "feature", "label": "Feature type"},
    {"key": "source", "label": "Source"},
    {"key": "completed_on", "label": "Completed on"},
    {"key": "description", "label": "Description"},
    {"key": "success_validation", "label": "Success validation"},
    {"key": "comments", "label": "Comments"},
    {"key": "screenshots", "label": "Screenshots"},
]
DEFAULT_UC_FIELDS = ["ref", "feature", "source", "completed_on", "description", "comments", "screenshots"]
_UC_FIELD_KEYS = {f["key"] for f in ALL_UC_FIELDS}


def _load_uc_view(db: Session, user: AppUser) -> dict[str, object]:
    """Load the user's use-case view prefs (visible fields + status filter)."""
    prefs: dict[str, object] = {"fields": DEFAULT_UC_FIELDS, "status_filter": "all"}
    row = (
        db.query(UseCaseViewPref)
        .filter(UseCaseViewPref.app_user_id == user.id)
        .one_or_none()
    )
    if row and row.config_json:
        try:
            stored = json.loads(row.config_json)
            if isinstance(stored.get("fields"), list):
                prefs["fields"] = [f for f in stored["fields"] if f in _UC_FIELD_KEYS]
            if stored.get("status_filter"):
                prefs["status_filter"] = str(stored["status_filter"])
        except (ValueError, TypeError):
            log.warning("uc_view_prefs_parse_failed", extra={"user": user.username})
    return prefs


def _filter_use_cases(
    use_cases: list[ProjectUseCase], status_filter: str
) -> list[ProjectUseCase]:
    """Apply the saved status filter: 'all', 'open' (not complete), or a status id."""
    if not status_filter or status_filter == "all":
        return use_cases
    if status_filter == "open":
        return [uc for uc in use_cases if not (uc.status and uc.status.is_complete_status)]
    if status_filter.isdigit():
        sid = int(status_filter)
        return [uc for uc in use_cases if uc.status_id == sid]
    return use_cases


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _get_viewable_project(db: Session, project_id: int, user: AppUser) -> Project:
    """Load a project the user is allowed to view, else 404.

    External viewers without a grant get a 404 (not 403) so they can't probe
    which project ids exist.
    """
    project = _get_project(db, project_id)
    if not can_view_project(db, user, project):
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
    # External viewers only see projects shared with them; internal users see all.
    visible_ids = accessible_project_ids(db, user)
    if visible_ids is not None:
        query = query.filter(Project.id.in_(visible_ids))
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
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    return render(
        request, "projects/form.html", current_user=user, active_section="projects",
        project=None, form={"customer_id": customer_id, "sales_engineer_id": user.id},
        form_action="/ui/projects/new", **_form_dropdowns(db),
    )


async def _read_project_form(request: Request) -> dict:
    form = await request.form()
    notes_html = sanitize_note_html(form.get("notes"))  # type: ignore[arg-type]
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
        "notebook_url": _clean_url(form.get("notebook_url")),  # type: ignore[arg-type]
        # Rich-text notes: store sanitized HTML + a plain-text rendering.
        "notes": html_to_text(notes_html),
        "notes_html": notes_html,
    }


@router.post("/new")
async def create_project(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
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
        notebook_url=data["notebook_url"],
        notes=data["notes"],
        notes_html=data["notes_html"],
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
    user: AppUser = Depends(require_internal_ui),
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
        "notebook_url": project.notebook_url,
        "notes": project.notes,
        "notes_html": project.notes_html,
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
    user: AppUser = Depends(require_internal_ui),
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
    project.notebook_url = data["notebook_url"]
    project.notes = data["notes"]
    project.notes_html = data["notes_html"]
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
    user: AppUser = Depends(require_internal_ui),
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
    user: AppUser = Depends(require_internal_ui),
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
    user: AppUser = Depends(require_internal_ui),
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
    project = _get_viewable_project(db, project_id, user)

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

    # Per-user view prefs: which fields to show and an optional status filter.
    uc_view = _load_uc_view(db, user)
    visible = _filter_use_cases(list(project.use_cases), str(uc_view["status_filter"]))

    # "Share" panel — only for users who can grant on this project (admin or the
    # project's SE). Loads current grantees + the external users available to add.
    can_share = can_grant_project(user, project)
    grants: list[ProjectGrant] = []
    grantable_users: list[AppUser] = []
    if can_share:
        grants = (
            db.query(ProjectGrant)
            .filter(ProjectGrant.project_id == project.id)
            .all()
        )
        granted_ids = {g.user_id for g in grants}
        grantable_users = [
            u
            for u in db.query(AppUser)
            .filter(AppUser.is_external.is_(True), AppUser.is_active.is_(True))
            .order_by(AppUser.username)
            .all()
            if u.id not in granted_ids
        ]

    return render(
        request, "projects/detail.html", current_user=user, active_section="projects",
        project=project, use_case_groups=_group_use_cases(visible),
        library_groups=lib_groups, uc_statuses=uc_statuses, feature_types=feature_types,
        progress={"total": total, "done": done, "pct": round(done / total * 100) if total else 0},
        today=date.today().isoformat(),
        uc_fields=set(uc_view["fields"]), uc_status_filter=str(uc_view["status_filter"]),
        uc_field_options=ALL_UC_FIELDS, uc_filtered_count=len(visible),
        can_share=can_share, grants=grants, grantable_users=grantable_users,
        ai_configured=default_provider(db) is not None,
    )


# ---------------------------------------------------------------------------
# Executive summary (AI-generated)
# ---------------------------------------------------------------------------


@router.post("/{project_id}/exec-summary/generate")
def generate_exec_summary(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    try:
        result = generate_project_summary(db, project)
    except GenerationError as exc:
        flash(request, f"Could not generate summary: {exc}", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}#summary", status_code=303)
    project.exec_summary = result.text
    project.exec_summary_html = result.html
    project.exec_summary_generated_at = datetime.now(UTC)
    project.exec_summary_model = result.model_label
    db.commit()
    record_event(
        category="project", event_type="exec_summary.generated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Generated executive summary for '{project.display_name}'",
        detail={"surface": "ui", "model": result.model_label}, request=request,
    )
    flash(request, "Executive summary generated.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#summary", status_code=303)


@router.post("/{project_id}/exec-summary/stream")
def stream_exec_summary(
    project_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Stream the summary live; the service persists it when the stream ends.

    Returns 400 if no provider is configured so the client can fall back to the
    plain (non-streaming) generate route, which surfaces a friendly flash.
    """
    project = _get_project(db, project_id)
    provider = default_provider(db)
    spec = get_provider_spec(provider.provider) if provider else None
    if provider is None or spec is None or not spec.implemented or not provider.has_key:
        return Response("AI provider not configured", status_code=400)
    return StreamingResponse(
        stream_project_summary(project.id), media_type="text/plain; charset=utf-8"
    )


@router.post("/{project_id}/exec-summary")
def save_exec_summary(
    project_id: int,
    request: Request,
    body: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    html = sanitize_note_html(body)
    text = html_to_text(html)
    project.exec_summary_html = html or None
    project.exec_summary = text or None
    db.commit()
    flash(request, "Executive summary updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#summary", status_code=303)


@router.post("/{project_id}/exec-summary/delete")
def delete_exec_summary(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    project.exec_summary = None
    project.exec_summary_html = None
    project.exec_summary_generated_at = None
    project.exec_summary_model = None
    db.commit()
    flash(request, "Executive summary cleared.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#summary", status_code=303)


# ---------------------------------------------------------------------------
# Import use cases from a requirements document (AI extraction)
# ---------------------------------------------------------------------------


@router.get("/{project_id}/import")
def import_form(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    return render(
        request, "projects/import.html", current_user=user, active_section="projects",
        project=project, ai_configured=default_provider(db) is not None,
    )


@router.post("/{project_id}/import/extract")
async def import_extract(
    project_id: int,
    request: Request,
    text: str = Form(""),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)

    combined = text or ""
    documents: list[dict] = []
    if file is not None and file.filename:
        raw = await file.read()
        if _is_native_doc(file.filename, file.content_type) and len(raw) <= _MAX_NATIVE_BYTES:
            # Send PDFs/images to the model natively — preserves tables and layout.
            documents.append(
                {
                    "media_type": _native_media_type(file.filename, file.content_type),
                    "data": base64.standard_b64encode(raw).decode("ascii"),
                }
            )
        else:
            try:
                extracted = extract_text(file.filename, raw, file.content_type)
            except TextExtractError as exc:
                flash(request, str(exc), "error")
                return RedirectResponse(url=f"/ui/projects/{project_id}/import", status_code=303)
            combined = (combined + "\n" + extracted).strip()

    try:
        candidates = extract_use_cases(
            db, combined, project=project, documents=documents or None
        )
    except GenerationError as exc:
        flash(request, f"Could not extract use cases: {exc}", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}/import", status_code=303)

    record_event(
        category="project", event_type="use_case.import_extracted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Extracted {len(candidates)} candidate use case(s) for '{project.display_name}'",
        detail={"surface": "ui", "count": len(candidates)}, request=request,
    )
    return render(
        request, "projects/import_preview.html", current_user=user,
        active_section="projects", project=project, candidates=candidates,
    )


@router.post("/{project_id}/import")
async def import_create(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    form = await request.form()
    selected = form.getlist("select")  # type: ignore[attr-defined]
    status_id = default_use_case_status_id(db)
    created = 0
    for idx in selected:
        name = _clean(form.get(f"name_{idx}"))  # type: ignore[arg-type]
        category = _clean(form.get(f"category_{idx}"))  # type: ignore[arg-type]
        if not name or not category:
            continue
        db.add(
            ProjectUseCase(
                project_id=project.id,
                source=SOURCE_CUSTOM,
                reference_number=_clean(form.get(f"ref_{idx}")),  # type: ignore[arg-type]
                category=category,
                name=name,
                description=_clean(form.get(f"desc_{idx}")),  # type: ignore[arg-type]
                success_validation=_clean(form.get(f"sv_{idx}")),  # type: ignore[arg-type]
                status_id=status_id,
            )
        )
        created += 1
    db.commit()
    record_event(
        category="project", event_type="use_case.imported", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Imported {created} use case(s) into '{project.display_name}'",
        detail={"surface": "ui", "count": created}, request=request,
    )
    flash(request, f"Imported {created} use case(s).", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


# ---------------------------------------------------------------------------
# Spreadsheet export / import of use cases (deterministic, no AI)
# ---------------------------------------------------------------------------

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(data: bytes, filename: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "use-cases"
    return Response(
        content=data, media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@router.get("/{project_id}/use-cases/export.xlsx")
def export_use_cases(
    project_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    return _xlsx_response(
        build_export_xlsx(project), f"{project.display_name}-use-cases.xlsx"
    )


@router.get("/{project_id}/use-cases/template.xlsx")
def use_case_template(
    project_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    _get_project(db, project_id)
    return _xlsx_response(build_template_xlsx(db), "use-case-import-template.xlsx")


@router.get("/{project_id}/use-cases/spreadsheet")
def spreadsheet_hub(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    return render(
        request, "projects/spreadsheet.html", current_user=user,
        active_section="projects", project=project,
    )


@router.post("/{project_id}/use-cases/spreadsheet/preview")
async def spreadsheet_preview(
    project_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    if not file or not file.filename:
        flash(request, "Choose a spreadsheet to import.", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}/use-cases/spreadsheet", status_code=303)
    raw = await file.read()
    try:
        rows = parse_spreadsheet(file.filename, raw)
    except SpreadsheetError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}/use-cases/spreadsheet", status_code=303)
    candidates = classify_rows(db, project, rows)
    if not candidates:
        flash(request, "No rows found in that file. Check it matches the template.", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}/use-cases/spreadsheet", status_code=303)
    return render(
        request, "projects/spreadsheet_preview.html", current_user=user,
        active_section="projects", project=project, candidates=candidates,
        new_count=sum(1 for c in candidates if c.action == "new" and c.valid),
        update_count=sum(1 for c in candidates if c.action == "update" and c.valid),
    )


@router.post("/{project_id}/use-cases/spreadsheet/apply")
async def spreadsheet_apply(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    form = await request.form()
    selected = form.getlist("select")  # type: ignore[attr-defined]
    default_status = default_use_case_status_id(db)
    created = updated = 0
    for idx in selected:
        name = _clean(form.get(f"name_{idx}"))  # type: ignore[arg-type]
        category = _clean(form.get(f"category_{idx}"))  # type: ignore[arg-type]
        if not name or not category:
            continue
        tid_raw = str(form.get(f"id_{idx}") or "")
        uc = db.get(ProjectUseCase, int(tid_raw)) if tid_raw.isdigit() else None
        if uc is None or uc.project_id != project.id:
            uc = ProjectUseCase(project_id=project.id, source=SOURCE_CUSTOM, status_id=default_status)
            db.add(uc)
            created += 1
        else:
            updated += 1
        uc.reference_number = _clean(form.get(f"ref_{idx}"))  # type: ignore[arg-type]
        uc.category = category
        uc.name = name
        uc.description = _clean(form.get(f"desc_{idx}"))  # type: ignore[arg-type]
        uc.success_validation = _clean(form.get(f"sv_{idx}"))  # type: ignore[arg-type]
        uc.comments = _clean(form.get(f"comments_{idx}"))  # type: ignore[arg-type]
        sid = str(form.get(f"status_id_{idx}") or "")
        if sid.isdigit():
            uc.status_id = int(sid)
        ft = str(form.get(f"feature_type_id_{idx}") or "")
        uc.feature_type_id = int(ft) if ft.isdigit() else None
        uc.completed_on = _parse_date(form.get(f"completed_{idx}"))  # type: ignore[arg-type]
    db.commit()
    record_event(
        category="project", event_type="use_case.spreadsheet_imported", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Spreadsheet import: {created} added, {updated} updated in '{project.display_name}'",
        detail={"surface": "ui", "created": created, "updated": updated}, request=request,
    )
    flash(request, f"Imported spreadsheet: {created} added, {updated} updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


@router.post("/{project_id}/use-case-view")
async def save_use_case_view(
    project_id: int,
    request: Request,
    status_filter: str = Form("all"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    form = await request.form()
    fields = [f["key"] for f in ALL_UC_FIELDS if form.get(f"field_{f['key']}")]
    config = {
        "fields": fields,  # empty list = show only name + status
        "status_filter": _clean(status_filter) or "all",
    }
    row = (
        db.query(UseCaseViewPref)
        .filter(UseCaseViewPref.app_user_id == user.id)
        .one_or_none()
    )
    if row is None:
        row = UseCaseViewPref(app_user_id=user.id)
        db.add(row)
    row.config_json = json.dumps(config)
    db.commit()
    flash(request, "Use-case view updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)


async def _store_note_attachments(
    db: Session,
    note: ProjectNote,
    files: list[UploadFile],
    request: Request,
) -> int:
    """Validate and persist uploaded files for a note. Flashes per-file errors
    and returns how many were saved. Caller commits the session."""
    saved = 0
    for f in files:
        if f is None or not f.filename:
            continue  # empty file part the browser may send when none selected
        if not note_store.is_allowed(f.filename):
            flash(request, f"'{f.filename}': unsupported file type.", "error")
            continue
        content = await f.read()
        if not content:
            continue
        if len(content) > note_store.MAX_SIZE_BYTES:
            flash(request, f"'{f.filename}' is too large (25 MB max).", "error")
            continue
        stored = note_store.store_bytes(content, f.filename)
        db.add(
            NoteAttachment(
                project_note_id=note.id,
                stored_filename=stored,
                original_filename=f.filename,
                content_type=f.content_type,
                size_bytes=len(content),
            )
        )
        saved += 1
    return saved


@router.post("/{project_id}/notes")
async def add_project_note(
    project_id: int,
    request: Request,
    body: str = Form(...),
    note_date: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _get_project(db, project_id)
    body_html = sanitize_note_html(body)
    text = html_to_text(body_html)
    if not text:
        flash(request, "Note text is required.", "error")
        return RedirectResponse(url=f"/ui/projects/{project.id}#notes", status_code=303)
    note = ProjectNote(
        project_id=project.id,
        note_date=_parse_date(note_date) or date.today(),
        body=text,
        body_html=body_html,
        created_by=user.username,
    )
    db.add(note)
    db.flush()  # assign note.id before attaching files
    await _store_note_attachments(db, note, files, request)
    db.commit()
    record_event(
        category="project", event_type="note.added", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Added a note to '{project.display_name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Note added.", "success")
    return RedirectResponse(url=f"/ui/projects/{project.id}#notes", status_code=303)


@router.post("/notes/{note_id}/attachments")
async def upload_note_attachments(
    note_id: int,
    request: Request,
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    saved = await _store_note_attachments(db, note, files, request)
    db.commit()
    if saved:
        record_event(
            category="project", event_type="note.attachment.added", actor_type="user",
            actor_label=user.username, actor_id=user.id, target_type="project",
            target_id=note.project_id, target_label=note.project.display_name,
            message=f"Attached {saved} file(s) to a note on '{note.project.display_name}'",
            detail={"surface": "ui", "count": saved}, request=request,
        )
        flash(request, f"Attached {saved} file(s).", "success")
    return RedirectResponse(url=f"/ui/projects/{note.project_id}#notes", status_code=303)


@router.get("/note-attachments/{att_id}")
def serve_note_attachment(
    att_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    att = db.get(NoteAttachment, att_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    _get_viewable_project(db, att.note.project_id, user)
    path = note_store.path_for(att)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Attachment file missing.")
    # Inline so images/PDFs open in the browser; other types download.
    return FileResponse(
        path,
        media_type=note_store.content_type_for(att),
        filename=att.original_filename or att.stored_filename,
        content_disposition_type="inline",
    )


@router.post("/note-attachments/{att_id}/delete")
def delete_note_attachment(
    att_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    att = db.get(NoteAttachment, att_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    project_id = att.note.project_id
    note_store.delete_file(att)
    db.delete(att)
    db.commit()
    record_event(
        category="project", event_type="note.attachment.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project_id, target_label=str(project_id),
        message="Removed a note attachment", detail={"surface": "ui"}, request=request,
    )
    flash(request, "Attachment removed.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#notes", status_code=303)


@router.post("/notes/{note_id}/edit")
def edit_project_note(
    note_id: int,
    request: Request,
    body: str = Form(...),
    note_date: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    body_html = sanitize_note_html(body)
    text = html_to_text(body_html)
    if not text:
        flash(request, "Note text is required.", "error")
        return RedirectResponse(url=f"/ui/projects/{note.project_id}#notes", status_code=303)
    note.body = text
    note.body_html = body_html
    note.note_date = _parse_date(note_date) or note.note_date
    db.commit()
    record_event(
        category="project", event_type="note.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=note.project_id, target_label=note.project.display_name,
        message=f"Edited a note on '{note.project.display_name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Note updated.", "success")
    return RedirectResponse(url=f"/ui/projects/{note.project_id}#notes", status_code=303)


@router.post("/notes/{note_id}/delete")
def delete_project_note(
    note_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    note = db.get(ProjectNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found.")
    project = note.project
    db.delete(note)
    db.commit()
    record_event(
        category="project", event_type="note.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Deleted a note from '{project.display_name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Note deleted.", "success")
    return RedirectResponse(url=f"/ui/projects/{project.id}#notes", status_code=303)


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


@router.post("/{project_id}/use-cases/from-library")
async def add_from_library(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
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
    user: AppUser = Depends(require_internal_ui),
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
    completed_on: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
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
    uc.completed_on = _parse_date(completed_on)
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
    user: AppUser = Depends(require_internal_ui),
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


@router.post("/{project_id}/use-cases/bulk")
async def bulk_use_cases(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Apply one change to many selected use cases at once."""
    project = _get_project(db, project_id)
    form = await request.form()
    action = form.get("action")
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]  # type: ignore[attr-defined]
    back = RedirectResponse(url=f"/ui/projects/{project_id}#use-cases", status_code=303)

    if not ids:
        flash(request, "No use cases were selected.", "error")
        return back
    # Scope strictly to this project's use cases.
    ucs = (
        db.query(ProjectUseCase)
        .filter(ProjectUseCase.project_id == project.id, ProjectUseCase.id.in_(ids))
        .all()
    )
    if not ucs:
        flash(request, "No matching use cases found.", "error")
        return back
    count = len(ucs)

    if action == "status":
        status_id = form.get("status_id")
        status = db.get(UseCaseStatus, int(status_id)) if str(status_id).isdigit() else None
        if status is None:
            flash(request, "Choose a status to apply.", "error")
            return back
        stamp = bool(form.get("stamp_today"))
        for uc in ucs:
            uc.status_id = status.id
            if stamp and status.is_complete_status and not uc.completed_on:
                uc.completed_on = date.today()
        summary = f"set {count} use case(s) to '{status.name}'"
    elif action == "feature_type":
        ft_raw = form.get("feature_type_id")
        ft_id = int(ft_raw) if str(ft_raw).isdigit() else None
        if ft_id is not None and db.get(FeatureType, ft_id) is None:
            flash(request, "That feature type no longer exists.", "error")
            return back
        for uc in ucs:
            uc.feature_type_id = ft_id
        summary = f"set the feature type on {count} use case(s)"
    elif action == "completed_on":
        when = _parse_date(form.get("completed_on"))  # type: ignore[arg-type]
        for uc in ucs:
            uc.completed_on = when
        summary = (
            f"set the completed-on date on {count} use case(s)"
            if when
            else f"cleared the completed-on date on {count} use case(s)"
        )
    elif action == "delete":
        for uc in ucs:
            for shot in uc.screenshots:
                screenshot_store.delete_file(shot)
            db.delete(uc)
        summary = f"deleted {count} use case(s)"
    else:
        flash(request, "Unknown bulk action.", "error")
        return back

    db.commit()
    record_event(
        category="project", event_type="use_case.bulk_updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project",
        target_id=project.id, target_label=project.display_name,
        message=f"Bulk {summary} in '{project.display_name}'",
        detail={"surface": "ui", "action": action, "count": count}, request=request,
    )
    flash(request, f"Bulk {summary}.", "success")
    return back


@router.post("/use-cases/{use_case_id}/delete")
def delete_use_case(
    use_case_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
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
    user: AppUser = Depends(require_internal_ui),
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
    user: AppUser = Depends(require_ui_user),
) -> Response:
    shot = db.get(Screenshot, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    _get_viewable_project(db, shot.use_case.project_id, user)
    path = screenshot_store.path_for(shot)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Screenshot file missing.")
    # Serve inline so clicking a thumbnail opens the image in the browser
    # instead of downloading it. content_disposition_type="inline" keeps
    # Starlette's proper filename encoding while avoiding a forced download.
    return FileResponse(
        path,
        media_type=shot.content_type or "application/octet-stream",
        filename=shot.original_filename or shot.stored_filename,
        content_disposition_type="inline",
    )


@router.post("/screenshots/{shot_id}/delete")
def delete_screenshot(
    shot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
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
