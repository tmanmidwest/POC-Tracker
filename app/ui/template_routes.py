"""HTML UI for managing POC templates — reusable blueprints for the New POC wizard."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.services.audit import record_event
from app.services.poc_templates import delete_template, get_template, list_templates
from app.ui.dependencies import require_internal_ui
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/templates", tags=["ui"], include_in_schema=False)


@router.get("/")
def list_view(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    return render(
        request, "templates/list.html", current_user=user, active_section="templates",
        templates=list_templates(db),
    )


@router.get("/{template_id}")
def detail_view(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return render(
        request, "templates/detail.html", current_user=user, active_section="templates",
        template=template,
    )


@router.post("/{template_id}/delete")
def delete_view(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    template = get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    name = template.name
    delete_template(db, template)
    db.commit()
    record_event(
        category="project", event_type="poc_template.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="poc_template",
        target_id=template_id, target_label=name,
        message=f"Deleted POC template '{name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Deleted template '{name}'.", "success")
    return RedirectResponse(url="/ui/templates", status_code=303)
