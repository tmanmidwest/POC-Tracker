"""Customer portal: a public, read-only status page for a project.

Two surfaces live here:

* **Public** — ``GET /portal/{token}`` renders the customer-facing status page.
  No authentication: anyone with the (high-entropy) link can view it. Unknown,
  disabled, or archived targets 404 uniformly so a link can't be probed.

* **Management** — ``POST /ui/projects/{id}/share-link/*`` create / enable /
  disable / rotate the link. Gated by the same ``can_grant_project`` check the
  grant routes use (an admin, or the project's sales engineer).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Project
from app.services import portal
from app.services.access import can_grant_project
from app.services.audit import record_event
from app.services.branding import current_branding
from app.ui.dependencies import require_internal_ui
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

# Public, unauthenticated surface.
public_router = APIRouter(prefix="/portal", tags=["portal"], include_in_schema=False)
# Authenticated management surface (co-located with the project share panel).
manage_router = APIRouter(prefix="/ui/projects", tags=["ui"], include_in_schema=False)


# --------------------------------------------------------------------------- #
# Public page
# --------------------------------------------------------------------------- #
@public_router.get("/{token}")
def public_status(token: str, request: Request, db: Session = Depends(get_db)) -> Response:
    link = portal.resolve_public(db, token)
    if link is None:
        raise HTTPException(status_code=404, detail="This status link is not available.")
    portal.record_view(db, link)
    db.commit()
    ctx = portal.public_context(link.project)
    return render(
        request,
        "portal/status.html",
        current_user=None,
        branding=current_branding(),
        **ctx,
    )


# --------------------------------------------------------------------------- #
# Management (share panel)
# --------------------------------------------------------------------------- #
def _require_can_share(db: Session, project_id: int, user: AppUser) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not can_grant_project(user, project):
        raise HTTPException(status_code=403, detail="Not allowed to share this project.")
    return project


def _back(project_id: int) -> RedirectResponse:
    return RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)


def _audit(request: Request, user: AppUser, project: Project, event: str, msg: str) -> None:
    record_event(
        category="project_share_link",
        event_type=f"project_share_link.{event}",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="project", target_id=project.id, target_label=project.display_name,
        message=msg,
        detail={"surface": "ui", "project_id": project.id},
        request=request,
    )


@manage_router.post("/{project_id}/share-link/enable")
def enable_link(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Create the link if absent, or re-enable an existing disabled one."""
    project = _require_can_share(db, project_id, user)
    link = portal.get_link(db, project_id)
    created = link is None
    link = portal.get_or_create_link(db, project, created_by=user.username)
    portal.set_enabled(db, link, True)
    db.commit()
    _audit(
        request, user, project,
        "created" if created else "enabled",
        f"{'Created' if created else 'Enabled'} public status link for "
        f"'{project.display_name}'",
    )
    flash(request, "Public status link is live.", "success")
    return _back(project_id)


@manage_router.post("/{project_id}/share-link/disable")
def disable_link(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _require_can_share(db, project_id, user)
    link = portal.get_link(db, project_id)
    if link is not None and link.is_enabled:
        portal.set_enabled(db, link, False)
        db.commit()
        _audit(request, user, project, "disabled",
               f"Disabled public status link for '{project.display_name}'")
    flash(request, "Public status link disabled.", "success")
    return _back(project_id)


@manage_router.post("/{project_id}/share-link/rotate")
def rotate_link(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Mint a new token. The old URL stops working immediately."""
    project = _require_can_share(db, project_id, user)
    link = portal.get_link(db, project_id)
    if link is None:
        flash(request, "No status link to regenerate yet.", "info")
        return _back(project_id)
    portal.rotate_token(db, link)
    db.commit()
    _audit(request, user, project, "rotated",
           f"Regenerated public status link for '{project.display_name}'")
    flash(request, "New link generated — the old URL no longer works.", "success")
    return _back(project_id)
