"""HTML UI for the four admin-managed lookup tables.

Driven by a small config map so all four share one list and one form template.
Each lookup protects its seed (`is_system`) rows from deletion and blocks
deleting a row that is still referenced by live data.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AppUser,
    ContactRole,
    FeatureType,
    ProjectStatus,
    TaskPriority,
    TaskStatus,
    UseCaseStatus,
)
from app.services.audit import record_event
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/lookups", tags=["ui"], include_in_schema=False)


# Field spec types: text, number, checkbox
LOOKUPS: dict[str, dict[str, Any]] = {
    "contact-roles": {
        "model": ContactRole,
        "title": "Contact Roles",
        "subtitle": "Roles a customer contact can hold on a POC.",
        "subsection": "contact_roles",
        "event_noun": "contact_role",
        "order_by": ContactRole.name,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
        ],
    },
    "project-statuses": {
        "model": ProjectStatus,
        "title": "Project Statuses",
        "subtitle": "Statuses a POC project can be in. The dashboard groups by these.",
        "subsection": "project_statuses",
        "event_noun": "project_status",
        "order_by": ProjectStatus.sort_order,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "sort_order", "label": "Sort order", "type": "number", "required": False},
            {"name": "is_terminal", "label": "Terminal (e.g. Won/Lost)", "type": "checkbox"},
        ],
    },
    "feature-types": {
        "model": FeatureType,
        "title": "Feature Types",
        "subtitle": "Feature / platform area a use case exercises (JML, ISPM, …).",
        "subsection": "feature_types",
        "event_noun": "feature_type",
        "order_by": FeatureType.name,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "description", "label": "Description", "type": "text", "required": False},
        ],
    },
    "use-case-statuses": {
        "model": UseCaseStatus,
        "title": "Use Case Statuses",
        "subtitle": "Status of a use case within a project.",
        "subsection": "use_case_statuses",
        "event_noun": "use_case_status",
        "order_by": UseCaseStatus.sort_order,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "sort_order", "label": "Sort order", "type": "number", "required": False},
            {"name": "is_complete_status", "label": "Counts as completed", "type": "checkbox"},
        ],
    },
    "task-statuses": {
        "model": TaskStatus,
        "title": "Task Statuses",
        "subtitle": "Statuses a task can be in. The task dashboard groups by these.",
        "subsection": "task_statuses",
        "event_noun": "task_status",
        "order_by": TaskStatus.sort_order,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "sort_order", "label": "Sort order", "type": "number", "required": False},
            {"name": "is_terminal", "label": "Terminal (e.g. Done)", "type": "checkbox"},
        ],
    },
    "task-priorities": {
        "model": TaskPriority,
        "title": "Task Priorities",
        "subtitle": "Priority levels selectable on a task (Low, High, Urgent, …).",
        "subsection": "task_priorities",
        "event_noun": "task_priority",
        "order_by": TaskPriority.sort_order,
        "fields": [
            {"name": "name", "label": "Name", "type": "text", "required": True},
            {"name": "sort_order", "label": "Sort order", "type": "number", "required": False},
            {"name": "color", "label": "Color (hex, e.g. #dc2626)", "type": "text", "required": False},
        ],
    },
}


def _cfg(slug: str) -> dict[str, Any]:
    cfg = LOOKUPS.get(slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Unknown lookup.")
    return cfg


def _coerce(field: dict[str, Any], form: Any) -> Any:
    name, ftype = field["name"], field["type"]
    if ftype == "checkbox":
        return bool(form.get(name))
    raw = form.get(name)
    raw = raw.strip() if isinstance(raw, str) else raw
    if ftype == "number":
        try:
            return int(raw) if raw not in (None, "") else 100
        except (ValueError, TypeError):
            return 100
    return raw or None


@router.get("")
@router.get("/")
def lookups_index(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Landing page (under Settings) linking to each lookup table."""
    return render(
        request, "lookups/index.html", current_user=user,
        active_section="settings", active_subsection="settings",
        lookups=LOOKUPS,
    )


@router.get("/{slug}")
def list_rows(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    rows = db.query(cfg["model"]).order_by(cfg["order_by"]).all()
    return render(
        request, "lookups/list.html", current_user=user,
        active_section="lookups", active_subsection=cfg["subsection"],
        slug=slug, cfg=cfg, rows=rows,
    )


@router.get("/{slug}/new")
def new_form(
    slug: str,
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    return render(
        request, "lookups/form.html", current_user=user,
        active_section="lookups", active_subsection=cfg["subsection"],
        slug=slug, cfg=cfg, row=None, form={"is_active": True},
        form_action=f"/ui/lookups/{slug}/new",
    )


@router.post("/{slug}/new")
async def create_row(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    form = await request.form()
    data = {f["name"]: _coerce(f, form) for f in cfg["fields"]}
    data["is_active"] = bool(form.get("is_active"))
    if cfg["fields"][0]["required"] and not data.get(cfg["fields"][0]["name"]):
        flash(request, f"{cfg['fields'][0]['label']} is required.", "error")
        return RedirectResponse(url=f"/ui/lookups/{slug}/new", status_code=303)
    row = cfg["model"](**data)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A row with that name already exists.", "error")
        return RedirectResponse(url=f"/ui/lookups/{slug}/new", status_code=303)
    record_event(
        category="lookup", event_type=f"lookup.{cfg['event_noun']}.created",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type=cfg["event_noun"], target_id=row.id, target_label=row.name,
        message=f"Created {cfg['title'][:-1].lower()} '{row.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"'{row.name}' added.", "success")
    return RedirectResponse(url=f"/ui/lookups/{slug}", status_code=303)


@router.get("/{slug}/{row_id}/edit")
def edit_form(
    slug: str,
    row_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    row = db.get(cfg["model"], row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    form = {f["name"]: getattr(row, f["name"]) for f in cfg["fields"]}
    form["is_active"] = row.is_active
    return render(
        request, "lookups/form.html", current_user=user,
        active_section="lookups", active_subsection=cfg["subsection"],
        slug=slug, cfg=cfg, row=row, form=form,
        form_action=f"/ui/lookups/{slug}/{row_id}/edit",
    )


@router.post("/{slug}/{row_id}/edit")
async def update_row(
    slug: str,
    row_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    row = db.get(cfg["model"], row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    form = await request.form()
    for f in cfg["fields"]:
        setattr(row, f["name"], _coerce(f, form))
    row.is_active = bool(form.get("is_active"))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A row with that name already exists.", "error")
        return RedirectResponse(url=f"/ui/lookups/{slug}/{row_id}/edit", status_code=303)
    record_event(
        category="lookup", event_type=f"lookup.{cfg['event_noun']}.updated",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type=cfg["event_noun"], target_id=row.id, target_label=row.name,
        message=f"Updated {cfg['title'][:-1].lower()} '{row.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Saved.", "success")
    return RedirectResponse(url=f"/ui/lookups/{slug}", status_code=303)


@router.post("/{slug}/{row_id}/delete")
def delete_row(
    slug: str,
    row_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    cfg = _cfg(slug)
    row = db.get(cfg["model"], row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Not found.")
    if getattr(row, "is_system", False):
        flash(request, f"'{row.name}' is a system default and can't be deleted.", "error")
        return RedirectResponse(url=f"/ui/lookups/{slug}", status_code=303)
    name = row.name
    try:
        db.delete(row)
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, f"Can't delete '{name}': it's still in use. Set it inactive instead.", "error")
        return RedirectResponse(url=f"/ui/lookups/{slug}", status_code=303)
    record_event(
        category="lookup", event_type=f"lookup.{cfg['event_noun']}.deleted",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type=cfg["event_noun"], target_id=row_id, target_label=name,
        message=f"Deleted {cfg['title'][:-1].lower()} '{name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"'{name}' deleted.", "success")
    return RedirectResponse(url=f"/ui/lookups/{slug}", status_code=303)
