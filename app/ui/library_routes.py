"""HTML UI for managing the use-case library (admin only)."""

from __future__ import annotations

import logging
import re
from itertools import groupby

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, FeatureType, UseCaseLibrary
from app.services.audit import record_event
from app.services.use_case_io import (
    LIBRARY_KEYS,
    SpreadsheetError,
    build_library_export_xlsx,
    build_library_template_xlsx,
    classify_library_rows,
    parse_spreadsheet,
)
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/library", tags=["ui"], include_in_schema=False)

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(data: bytes, filename: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-") or "library"
    return Response(
        content=data, media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


def _feature_types(db: Session) -> list[FeatureType]:
    return (
        db.query(FeatureType)
        .filter(FeatureType.is_active.is_(True))
        .order_by(FeatureType.name)
        .all()
    )


@router.get("/")
def list_library(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entries = (
        db.query(UseCaseLibrary)
        .order_by(UseCaseLibrary.category, UseCaseLibrary.default_reference_number)
        .all()
    )
    groups = [
        {"category": cat, "entries": list(items)}
        for cat, items in groupby(entries, key=lambda e: e.category)
    ]
    return render(
        request, "library/list.html", current_user=user, active_section="library",
        groups=groups, total=len(entries),
    )


# ---------------------------------------------------------------------------
# Spreadsheet import / export
# ---------------------------------------------------------------------------


@router.get("/export.xlsx")
def export_library(
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return _xlsx_response(build_library_export_xlsx(db), "use-case-library.xlsx")


@router.get("/template.xlsx")
def library_template(
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return _xlsx_response(build_library_template_xlsx(db), "use-case-library-template.xlsx")


@router.get("/spreadsheet")
def spreadsheet_hub(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request, "library/spreadsheet.html", current_user=user,
        active_section="library",
    )


@router.post("/spreadsheet/preview")
async def spreadsheet_preview(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if not file or not file.filename:
        flash(request, "Choose a spreadsheet to import.", "error")
        return RedirectResponse(url="/ui/library/spreadsheet", status_code=303)
    raw = await file.read()
    try:
        rows = parse_spreadsheet(file.filename, raw, keys=LIBRARY_KEYS)
    except SpreadsheetError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url="/ui/library/spreadsheet", status_code=303)
    candidates = classify_library_rows(db, rows)
    if not candidates:
        flash(request, "No rows found in that file. Check it matches the template.", "error")
        return RedirectResponse(url="/ui/library/spreadsheet", status_code=303)
    return render(
        request, "library/spreadsheet_preview.html", current_user=user,
        active_section="library", candidates=candidates,
        new_count=sum(1 for c in candidates if c.action == "new" and c.valid),
        update_count=sum(1 for c in candidates if c.action == "update" and c.valid),
    )


@router.post("/spreadsheet/apply")
async def spreadsheet_apply(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    form = await request.form()
    selected = form.getlist("select")  # type: ignore[attr-defined]
    created = updated = 0
    for idx in selected:
        name = _clean(form.get(f"name_{idx}"))  # type: ignore[arg-type]
        category = _clean(form.get(f"category_{idx}"))  # type: ignore[arg-type]
        if not name or not category:
            continue
        tid_raw = str(form.get(f"id_{idx}") or "")
        entry = db.get(UseCaseLibrary, int(tid_raw)) if tid_raw.isdigit() else None
        if entry is None:
            entry = UseCaseLibrary(category=category, name=name)
            db.add(entry)
            created += 1
        else:
            updated += 1
        entry.category = category
        entry.name = name
        entry.default_reference_number = _clean(form.get(f"ref_{idx}"))  # type: ignore[arg-type]
        entry.description = _clean(form.get(f"desc_{idx}"))  # type: ignore[arg-type]
        entry.success_validation = _clean(form.get(f"sv_{idx}"))  # type: ignore[arg-type]
        ft = str(form.get(f"feature_type_id_{idx}") or "")
        entry.feature_type_id = int(ft) if ft.isdigit() else None
        entry.is_active = str(form.get(f"active_{idx}") or "") == "1"
    db.commit()
    record_event(
        category="library", event_type="library.spreadsheet_imported", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="use_case_library",
        target_id=None, target_label="library",
        message=f"Library spreadsheet import: {created} added, {updated} updated",
        detail={"surface": "ui", "created": created, "updated": updated}, request=request,
    )
    flash(request, f"Imported library: {created} added, {updated} updated.", "success")
    return RedirectResponse(url="/ui/library", status_code=303)


@router.get("/new")
def new_form(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request, "library/form.html", current_user=user, active_section="library",
        entry=None, form={}, form_action="/ui/library/new", feature_types=_feature_types(db),
    )


@router.post("/new")
def create_entry(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    default_reference_number: str | None = Form(None),
    description: str | None = Form(None),
    success_validation: str | None = Form(None),
    feature_type_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if not _clean(category) or not _clean(name):
        flash(request, "Category and name are required.", "error")
        return RedirectResponse(url="/ui/library/new", status_code=303)
    entry = UseCaseLibrary(
        category=_clean(category),
        name=_clean(name),
        default_reference_number=_clean(default_reference_number),
        description=_clean(description),
        success_validation=_clean(success_validation),
        feature_type_id=int(feature_type_id) if feature_type_id else None,
        is_active=True,
    )
    db.add(entry)
    db.commit()
    record_event(
        category="library", event_type="library.use_case.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="use_case_library",
        target_id=entry.id, target_label=entry.name,
        message=f"Created library use case '{entry.name}'",
        detail={"surface": "ui", "category": entry.category}, request=request,
    )
    flash(request, f"Added '{entry.name}' to the library.", "success")
    return RedirectResponse(url="/ui/library", status_code=303)


@router.get("/{entry_id}/edit")
def edit_form(
    entry_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    form = {
        "category": entry.category,
        "name": entry.name,
        "default_reference_number": entry.default_reference_number,
        "description": entry.description,
        "success_validation": entry.success_validation,
        "feature_type_id": entry.feature_type_id,
        "is_active": entry.is_active,
    }
    return render(
        request, "library/form.html", current_user=user, active_section="library",
        entry=entry, form=form, form_action=f"/ui/library/{entry_id}/edit",
        feature_types=_feature_types(db),
    )


@router.post("/{entry_id}/edit")
def update_entry(
    entry_id: int,
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    default_reference_number: str | None = Form(None),
    description: str | None = Form(None),
    success_validation: str | None = Form(None),
    feature_type_id: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    entry.category = _clean(category) or entry.category
    entry.name = _clean(name) or entry.name
    entry.default_reference_number = _clean(default_reference_number)
    entry.description = _clean(description)
    entry.success_validation = _clean(success_validation)
    entry.feature_type_id = int(feature_type_id) if feature_type_id else None
    entry.is_active = bool(is_active)
    db.commit()
    record_event(
        category="library", event_type="library.use_case.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="use_case_library",
        target_id=entry.id, target_label=entry.name,
        message=f"Updated library use case '{entry.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Library use case updated.", "success")
    return RedirectResponse(url="/ui/library", status_code=303)


@router.post("/{entry_id}/delete")
def delete_entry(
    entry_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entry = db.get(UseCaseLibrary, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library use case not found.")
    name = entry.name
    # Project use cases already copied from this entry are unaffected (snapshots).
    db.delete(entry)
    db.commit()
    record_event(
        category="library", event_type="library.use_case.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="use_case_library",
        target_id=entry_id, target_label=name,
        message=f"Deleted library use case '{name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Removed '{name}' from the library.", "success")
    return RedirectResponse(url="/ui/library", status_code=303)
