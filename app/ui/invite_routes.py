"""Public (unauthenticated) invitation-accept pages.

An invited external user opens the emailed link, sets a password, and is logged
in. These routes must be reachable while logged out, so this router is mounted
with no auth dependency. See docs/INVITATIONS.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import invitations
from app.services.audit import record_event
from app.services.auth import SESSION_USER_ID_KEY, SESSION_USERNAME_KEY
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/invite", tags=["ui"], include_in_schema=False)


@router.get("/{token}")
def show_accept(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Show the set-password form for a valid invite, or an 'invalid' page."""
    invite = invitations.verify_token(db, token)
    if invite is None:
        return render(request, "invite_invalid.html")
    return render(
        request,
        "invite_accept.html",
        token=token,
        invite=invite,
        project=invite.project,
    )


@router.post("/{token}")
def do_accept(
    token: str,
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Accept the invite: set the password, log the user in, land on their project."""
    invite = invitations.verify_token(db, token)
    if invite is None:
        return render(request, "invite_invalid.html")

    def _reject(message: str) -> Response:
        return render(
            request, "invite_accept.html", token=token, invite=invite,
            project=invite.project, error=message,
        )

    if password != confirm:
        return _reject("The passwords don't match.")
    try:
        user = invitations.accept_invite(db, invite, password=password)
    except invitations.InvitationError as exc:
        return _reject(str(exc))

    # Log the new user in (same session mechanics as the login form).
    request.session[SESSION_USER_ID_KEY] = user.id
    request.session[SESSION_USERNAME_KEY] = user.username
    user.last_login_at = datetime.now(UTC)
    db.commit()

    record_event(
        category="invitation",
        event_type="invitation.accepted",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="user_invite",
        target_id=invite.id,
        target_label=user.email or user.username,
        message=f"{user.username} accepted an invitation",
        detail={"surface": "ui", "project_id": invite.project_id},
        request=request,
    )

    dest = (
        f"/ui/projects/{invite.project_id}"
        if invite.project_id
        else "/ui/dashboard"
    )
    flash(request, "Welcome! Your account is ready.", "success")
    return RedirectResponse(url=dest, status_code=303)
