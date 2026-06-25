"""HTML UI for managing the use-case library (admin only)."""

from __future__ import annotations

import logging
from itertools import groupby

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, FeatureType, UseCaseLibrary
from app.services.audit import record_event
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/library", tags=["ui"], include_in_schema=False)


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
