"""HTML UI for managing use-case libraries (admin only).

Entries live in named libraries (``LibrarySet``). Most views are scoped to one
library via a ``?set=<id>`` query param; without it the default library is used.
A separate "manage libraries" page handles creating/renaming/deleting libraries.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import date
from itertools import groupby

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, FeatureType, LibrarySet, UseCaseLibrary
from app.services import report_pdf
from app.services.audit import record_event
from app.services.branding import current_branding
from app.services.library_sets import (
    default_library_set,
    entry_count,
    list_library_sets,
    resolve_library_set,
)
from app.services.use_case_io import (
    LIBRARY_KEYS,
    SpreadsheetError,
    build_library_export_xlsx,
    build_library_presentation_xlsx,
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
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            # Freshly generated each request — never let the browser serve a stale copy.
            "Cache-Control": "no-store, must-revalidate",
        },
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


def _set_url(base: str, set_id: int | None) -> str:
    return f"{base}?set={set_id}" if set_id else base


@router.get("/")
def list_library(
    request: Request,
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    sets = list_library_sets(db)
    current = resolve_library_set(db, set_id)
    groups: list[dict] = []
    total = 0
    if current is not None:
        entries = (
            db.query(UseCaseLibrary)
            .filter(UseCaseLibrary.library_set_id == current.id)
            .order_by(UseCaseLibrary.category, UseCaseLibrary.default_reference_number)
            .all()
        )
        total = len(entries)
        groups = [
            {"category": cat, "entries": list(items)}
            for cat, items in groupby(entries, key=lambda e: e.category)
        ]
    set_counts = {s.id: entry_count(db, s.id) for s in sets}
    return render(
        request, "library/list.html", current_user=user, active_section="library",
        groups=groups, total=total, sets=sets, current_set=current, set_counts=set_counts,
        feature_types=_feature_types(db),
    )


# ---------------------------------------------------------------------------
# Manage libraries (the named sets)
# ---------------------------------------------------------------------------


@router.get("/sets")
def manage_sets(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    sets = list_library_sets(db)
    set_counts = {s.id: entry_count(db, s.id) for s in sets}
    return render(
        request, "library/sets.html", current_user=user, active_section="library",
        sets=sets, set_counts=set_counts,
    )


@router.post("/sets")
def create_set(
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    clean_name = _clean(name)
    if not clean_name:
        flash(request, "A library name is required.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    if db.query(LibrarySet).filter(LibrarySet.name == clean_name).first() is not None:
        flash(request, f"A library named '{clean_name}' already exists.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    entry = LibrarySet(name=clean_name, description=_clean(description), is_active=True)
    db.add(entry)
    db.commit()
    record_event(
        category="library", event_type="library.set.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="library_set",
        target_id=entry.id, target_label=entry.name,
        message=f"Created library '{entry.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Created library '{entry.name}'.", "success")
    return RedirectResponse(url=_set_url("/ui/library", entry.id), status_code=303)


@router.post("/sets/{set_id}/edit")
def update_set(
    set_id: int,
    request: Request,
    name: str = Form(...),
    description: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entry = db.get(LibrarySet, set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library not found.")
    clean_name = _clean(name)
    if not clean_name:
        flash(request, "A library name is required.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    clash = db.query(LibrarySet).filter(LibrarySet.name == clean_name).first()
    if clash is not None and clash.id != set_id:
        flash(request, f"A library named '{clean_name}' already exists.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    entry.name = clean_name
    entry.description = _clean(description)
    entry.is_active = bool(is_active)
    db.commit()
    record_event(
        category="library", event_type="library.set.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="library_set",
        target_id=entry.id, target_label=entry.name,
        message=f"Updated library '{entry.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Library updated.", "success")
    return RedirectResponse(url="/ui/library/sets", status_code=303)


@router.post("/sets/{set_id}/delete")
def delete_set(
    set_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    entry = db.get(LibrarySet, set_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Library not found.")
    if entry.is_default:
        flash(request, f"'{entry.name}' is the default library and can't be deleted.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    count = entry_count(db, set_id)
    if count:
        flash(
            request,
            f"'{entry.name}' still has {count} use case(s). Move or delete them first.",
            "error",
        )
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    name = entry.name
    db.delete(entry)
    db.commit()
    record_event(
        category="library", event_type="library.set.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="library_set",
        target_id=set_id, target_label=name,
        message=f"Deleted library '{name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Deleted library '{name}'.", "success")
    return RedirectResponse(url="/ui/library/sets", status_code=303)


# ---------------------------------------------------------------------------
# Spreadsheet import / export (scoped to one library)
# ---------------------------------------------------------------------------


@router.get("/export.xlsx")
def export_library(
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    current = resolve_library_set(db, set_id)
    if current is None:
        raise HTTPException(status_code=404, detail="No library to export.")
    return _xlsx_response(
        build_library_export_xlsx(db, current.id),
        _dated(f"{_slug(current.name)}-library", "xlsx"),
    )


@router.get("/template.xlsx")
def library_template(
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return _xlsx_response(build_library_template_xlsx(db), "use-case-library-template.xlsx")


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "library"


def _dated(stem: str, ext: str) -> str:
    """Append the export date (MMDDYYYY) and a random 4-digit token before the
    extension, e.g. ...-06292026-4823.pdf — keeps every export filename unique
    so a browser never serves a stale cached download."""
    return f"{stem}-{date.today().strftime('%m%d%Y')}-{random.randint(1000, 9999)}.{ext}"


@router.get("/export.pdf")
def export_library_pdf(
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """A polished, branded PDF of one library (active use cases), for sharing."""
    current = resolve_library_set(db, set_id)
    if current is None:
        raise HTTPException(status_code=404, detail="No library to export.")
    entries = (
        db.query(UseCaseLibrary)
        .filter(
            UseCaseLibrary.library_set_id == current.id,
            UseCaseLibrary.is_active.is_(True),
        )
        .order_by(UseCaseLibrary.category, UseCaseLibrary.default_reference_number)
        .all()
    )
    groups = [
        {"category": cat, "entries": list(items)}
        for cat, items in groupby(entries, key=lambda e: e.category)
    ]
    html = report_pdf.render_library_html(
        {
            "library": current,
            "groups": groups,
            "total": len(entries),
            "full": True,
            "branding": current_branding(),
            "generated_on": date.today().strftime("%b %-d, %Y"),
        }
    )
    try:
        pdf = report_pdf.library_pdf(html)
    except Exception:  # pragma: no cover - depends on system libs
        log.exception("library_pdf_failed", extra={"library_set_id": current.id})
        raise HTTPException(status_code=500, detail="PDF generation failed.") from None
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_dated(_slug(current.name) + "-use-cases", "pdf")}"',
            "Cache-Control": "no-store, must-revalidate",
        },
    )


@router.get("/formatted.xlsx")
def export_library_formatted(
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """A styled, read-only .xlsx of one library (active use cases), for sharing."""
    current = resolve_library_set(db, set_id)
    if current is None:
        raise HTTPException(status_code=404, detail="No library to export.")
    data = build_library_presentation_xlsx(db, current)
    return _xlsx_response(data, _dated(f"{_slug(current.name)}-use-cases", "xlsx"))


@router.get("/spreadsheet")
def spreadsheet_hub(
    request: Request,
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    current = resolve_library_set(db, set_id)
    if current is None:
        flash(request, "Create a library first.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    resp = render(
        request, "library/spreadsheet.html", current_user=user,
        active_section="library", current_set=current,
        # A per-render token appended to the export links so each download is a
        # distinct URL — defeats Safari's URL-keyed download cache.
        cache_bust=random.randint(100000, 999999),
    )
    # Don't let the browser restore this upload page (and its stale file input)
    # from the back/forward cache — a re-import must re-read the chosen file.
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@router.post("/spreadsheet/preview")
async def spreadsheet_preview(
    request: Request,
    file: UploadFile = File(...),
    library_set_id: int = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    current = db.get(LibrarySet, library_set_id)
    if current is None:
        flash(request, "Unknown library.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    hub_url = _set_url("/ui/library/spreadsheet", current.id)
    if not file or not file.filename:
        flash(request, "Choose a spreadsheet to import.", "error")
        return RedirectResponse(url=hub_url, status_code=303)
    raw = await file.read()
    try:
        rows = parse_spreadsheet(file.filename, raw, keys=LIBRARY_KEYS)
    except SpreadsheetError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=hub_url, status_code=303)
    candidates = classify_library_rows(db, rows, library_set_id=current.id)
    if not candidates:
        flash(request, "No rows found in that file. Check it matches the template.", "error")
        return RedirectResponse(url=hub_url, status_code=303)
    return render(
        request, "library/spreadsheet_preview.html", current_user=user,
        active_section="library", candidates=candidates, current_set=current,
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
    set_raw = str(form.get("library_set_id") or "")  # type: ignore[arg-type]
    current = db.get(LibrarySet, int(set_raw)) if set_raw.isdigit() else None
    if current is None:
        flash(request, "Unknown library.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    selected = form.getlist("select")  # type: ignore[attr-defined]
    created = updated = 0
    for idx in selected:
        name = _clean(form.get(f"name_{idx}"))  # type: ignore[arg-type]
        category = _clean(form.get(f"category_{idx}"))  # type: ignore[arg-type]
        if not name or not category:
            continue
        tid_raw = str(form.get(f"id_{idx}") or "")
        entry = db.get(UseCaseLibrary, int(tid_raw)) if tid_raw.isdigit() else None
        # Only update entries that belong to this library; otherwise add as new.
        if entry is not None and entry.library_set_id != current.id:
            entry = None
        if entry is None:
            entry = UseCaseLibrary(category=category, name=name, library_set_id=current.id)
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
        actor_label=user.username, actor_id=user.id, target_type="library_set",
        target_id=current.id, target_label=current.name,
        message=f"Library spreadsheet import into '{current.name}': "
        f"{created} added, {updated} updated",
        detail={"surface": "ui", "created": created, "updated": updated,
                "library_set_id": current.id}, request=request,
    )
    flash(request, f"Imported into '{current.name}': {created} added, {updated} updated.", "success")
    return RedirectResponse(url=_set_url("/ui/library", current.id), status_code=303)


# ---------------------------------------------------------------------------
# Library entry CRUD
# ---------------------------------------------------------------------------


@router.get("/new")
def new_form(
    request: Request,
    set_id: int | None = Query(default=None, alias="set"),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    sets = list_library_sets(db)
    current = resolve_library_set(db, set_id)
    if current is None:
        flash(request, "Create a library first.", "error")
        return RedirectResponse(url="/ui/library/sets", status_code=303)
    return render(
        request, "library/form.html", current_user=user, active_section="library",
        entry=None, form={"library_set_id": current.id}, form_action="/ui/library/new",
        feature_types=_feature_types(db), sets=sets,
    )


@router.post("/new")
def create_entry(
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    library_set_id: int = Form(...),
    default_reference_number: str | None = Form(None),
    description: str | None = Form(None),
    success_validation: str | None = Form(None),
    feature_type_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    if db.get(LibrarySet, library_set_id) is None:
        flash(request, "Choose a valid library.", "error")
        return RedirectResponse(url="/ui/library/new", status_code=303)
    if not _clean(category) or not _clean(name):
        flash(request, "Category and name are required.", "error")
        return RedirectResponse(url=_set_url("/ui/library/new", library_set_id), status_code=303)
    entry = UseCaseLibrary(
        library_set_id=library_set_id,
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
        detail={"surface": "ui", "category": entry.category,
                "library_set_id": entry.library_set_id}, request=request,
    )
    flash(request, f"Added '{entry.name}' to the library.", "success")
    return RedirectResponse(url=_set_url("/ui/library", entry.library_set_id), status_code=303)


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
        "library_set_id": entry.library_set_id,
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
        feature_types=_feature_types(db), sets=list_library_sets(db),
    )


@router.post("/{entry_id}/edit")
def update_entry(
    entry_id: int,
    request: Request,
    category: str = Form(...),
    name: str = Form(...),
    library_set_id: int = Form(...),
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
    moved_from = entry.library_set_id
    if db.get(LibrarySet, library_set_id) is None:
        flash(request, "Choose a valid library.", "error")
        return RedirectResponse(url=f"/ui/library/{entry_id}/edit", status_code=303)
    entry.library_set_id = library_set_id
    entry.category = _clean(category) or entry.category
    entry.name = _clean(name) or entry.name
    entry.default_reference_number = _clean(default_reference_number)
    entry.description = _clean(description)
    entry.success_validation = _clean(success_validation)
    entry.feature_type_id = int(feature_type_id) if feature_type_id else None
    entry.is_active = bool(is_active)
    db.commit()
    moved = moved_from != library_set_id
    record_event(
        category="library",
        event_type="library.use_case.moved" if moved else "library.use_case.updated",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="use_case_library", target_id=entry.id, target_label=entry.name,
        message=(
            f"Moved library use case '{entry.name}' to another library"
            if moved else f"Updated library use case '{entry.name}'"
        ),
        detail={"surface": "ui", "from_library_set_id": moved_from,
                "to_library_set_id": library_set_id}, request=request,
    )
    flash(request, "Library use case moved." if moved else "Library use case updated.", "success")
    return RedirectResponse(url=_set_url("/ui/library", entry.library_set_id), status_code=303)


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
    set_id = entry.library_set_id
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
    return RedirectResponse(url=_set_url("/ui/library", set_id), status_code=303)


@router.post("/bulk")
async def bulk_entries(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Apply one change to many selected library use cases at once."""
    form = await request.form()
    action = form.get("action")
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]  # type: ignore[attr-defined]
    cur_raw = str(form.get("current_set_id") or "")  # type: ignore[arg-type]
    back = RedirectResponse(
        url=_set_url("/ui/library", int(cur_raw) if cur_raw.isdigit() else None),
        status_code=303,
    )

    if not ids:
        flash(request, "No use cases were selected.", "error")
        return back
    entries = db.query(UseCaseLibrary).filter(UseCaseLibrary.id.in_(ids)).all()
    if not entries:
        flash(request, "No matching use cases found.", "error")
        return back
    count = len(entries)
    detail: dict = {"surface": "ui", "action": action, "count": count}

    if action == "category":
        new_category = _clean(form.get("category"))  # type: ignore[arg-type]
        if not new_category:
            flash(request, "Enter a category to apply.", "error")
            return back
        for e in entries:
            e.category = new_category
        summary = f"set the category on {count} use case(s) to '{new_category}'"
    elif action == "feature_type":
        ft_raw = str(form.get("feature_type_id") or "")  # type: ignore[arg-type]
        ft_id = int(ft_raw) if ft_raw.isdigit() else None
        if ft_id is not None and db.get(FeatureType, ft_id) is None:
            flash(request, "That feature type no longer exists.", "error")
            return back
        for e in entries:
            e.feature_type_id = ft_id
        summary = f"set the feature type on {count} use case(s)"
    elif action == "active":
        make_active = str(form.get("is_active") or "") == "1"
        for e in entries:
            e.is_active = make_active
        summary = f"{'activated' if make_active else 'deactivated'} {count} use case(s)"
    elif action == "move":
        tgt_raw = str(form.get("target_set_id") or "")  # type: ignore[arg-type]
        target = db.get(LibrarySet, int(tgt_raw)) if tgt_raw.isdigit() else None
        if target is None:
            flash(request, "Choose a library to move them into.", "error")
            return back
        for e in entries:
            e.library_set_id = target.id
        detail["target_set_id"] = target.id
        summary = f"moved {count} use case(s) to '{target.name}'"
    elif action == "delete":
        # Project use cases already copied from these are unaffected (snapshots).
        for e in entries:
            db.delete(e)
        summary = f"deleted {count} use case(s)"
    else:
        flash(request, "Unknown bulk action.", "error")
        return back

    db.commit()
    record_event(
        category="library", event_type="library.use_case.bulk_updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="use_case_library",
        target_id=None, target_label="library",
        message=f"Bulk {summary}", detail=detail, request=request,
    )
    flash(request, f"Bulk {summary}.", "success")
    return back
