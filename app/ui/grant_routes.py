"""HTML UI for sharing a project with external viewers (per-project grants).

Admins can share any project; a project's assigned sales engineer can share
their own. Both routes self-check :func:`can_grant_project` rather than relying
on a router-level dependency, so a non-admin user who is *not* the assigned SE of a given
project cannot grant on it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AppUser, Project, ProjectGrant
from app.models.project_grant import TIER_VIEWER
from app.services import email as email_service
from app.services import invitations
from app.services.access import can_grant_project
from app.services.audit import record_event
from app.ui.dependencies import require_internal_ui
from app.ui.flash import flash

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/projects", tags=["ui"], include_in_schema=False)


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


def _require_can_grant(db: Session, project_id: int, user: AppUser) -> Project:
    project = _project_or_404(db, project_id)
    if not can_grant_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not allowed to share this project.")
    return project


@router.post("/{project_id}/grants")
def add_grant(
    project_id: int,
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _require_can_grant(db, project_id, user)
    target = db.get(AppUser, user_id)
    if target is None:
        flash(request, "That user no longer exists.", "error")
        return RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)

    existing = (
        db.query(ProjectGrant)
        .filter(
            ProjectGrant.project_id == project.id,
            ProjectGrant.user_id == target.id,
        )
        .first()
    )
    if existing is not None:
        flash(request, f"{target.display_label} already has access.", "info")
        return RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)

    grant = ProjectGrant(
        project_id=project.id,
        user_id=target.id,
        tier=TIER_VIEWER,
        granted_by_user_id=user.id,
    )
    db.add(grant)
    db.commit()
    record_event(
        category="project_grant", event_type="project_grant.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_grant",
        target_id=grant.id, target_label=target.display_label,
        message=f"Granted {target.display_label} read access to '{project.display_name}'",
        detail={"surface": "ui", "project_id": project.id, "user_id": target.id},
        request=request,
    )
    flash(request, f"Shared with {target.display_label}.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)


@router.post("/{project_id}/invite")
def invite_external(
    project_id: int,
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    company: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Invite an external viewer by email: provisions them, grants this project,
    and emails a set-password link. Available to whoever can share the project."""
    project = _require_can_grant(db, project_id, user)
    back = RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)
    # Prefer the configured public URL; fall back to the request's own base.
    base_url = get_settings().public_base_url or str(request.base_url).rstrip("/")
    try:
        invitations.create_invite(
            db,
            email=email,
            name=name.strip() or None,
            company=company.strip() or None,
            project=project,
            invited_by=user,
            base_url=base_url,
        )
    except invitations.InvitationError as exc:
        flash(request, f"Couldn't send invitation: {exc}", "error")
        return back
    except email_service.EmailError as exc:
        # The account + grant were created, but the email didn't go out.
        record_event(
            category="invitation",
            event_type="invitation.email_failed",
            outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="project", target_id=project.id, target_label=project.name,
            message=f"Invitation email to {email.strip().lower()} failed to send",
            detail={
                "surface": "ui",
                "recipient": email.strip().lower(),
                "error": str(exc),
                "note": "access was granted; email delivery failed",
            },
            request=request,
        )
        flash(
            request,
            f"Access granted, but the invitation email failed to send ({exc}). "
            "Configure Settings → Email, then resend from Settings → Users.",
            "error",
        )
        return back
    flash(request, f"Invitation sent to {email.strip().lower()}.", "success")
    return back


@router.post("/{project_id}/grants/{grant_id}/delete")
def revoke_grant(
    project_id: int,
    grant_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    project = _require_can_grant(db, project_id, user)
    grant = db.get(ProjectGrant, grant_id)
    if grant is None or grant.project_id != project.id:
        raise HTTPException(status_code=404, detail="Grant not found.")
    label = grant.user.display_label if grant.user else str(grant.user_id)
    target_user_id = grant.user_id
    db.delete(grant)
    db.commit()
    record_event(
        category="project_grant", event_type="project_grant.revoked", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="project_grant",
        target_id=grant_id, target_label=label,
        message=f"Revoked {label}'s access to '{project.display_name}'",
        detail={"surface": "ui", "project_id": project.id, "user_id": target_user_id},
        request=request,
    )
    flash(request, f"Access removed for {label}.", "success")
    return RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)


@router.post("/{project_id}/external/{user_id}/extend")
def extend_external_access(
    project_id: int,
    user_id: int,
    request: Request,
    preset: str = Form(""),
    until: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_internal_ui),
) -> Response:
    """Extend an external viewer's account expiry from a project's share panel.

    Available to whoever can share the project (its SE or an admin), and only for
    a viewer actually granted this project.
    """
    from app.services import external_expiry

    project = _require_can_grant(db, project_id, user)
    back = RedirectResponse(url=f"/ui/projects/{project_id}#share", status_code=303)
    target = db.get(AppUser, user_id)
    grant = (
        db.query(ProjectGrant)
        .filter(ProjectGrant.project_id == project.id, ProjectGrant.user_id == user_id)
        .first()
    )
    if target is None or not target.is_external or grant is None:
        raise HTTPException(status_code=404, detail="External viewer not found on this project.")
    try:
        new_expiry = external_expiry.resolve_extension(preset or None, until or None)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return back
    external_expiry.extend_user(db, target, until=new_expiry, actor=user, request=request)
    flash(
        request,
        f"{target.display_label} now expires {new_expiry.date().isoformat()}.",
        "success",
    )
    return back
