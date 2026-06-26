"""HTML login and logout endpoints for the web UI.

These coexist with the JSON-based session_auth endpoints under /api/v1/auth.
The UI ones live under /ui/ for a form-encoded flow.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, AuthProvider
from app.services.audit import record_event
from app.services.auth import (
    SESSION_USER_ID_KEY,
    SESSION_USERNAME_KEY,
    get_optional_user,
)
from app.services.passwords import verify_password
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


def _enabled_providers(db: Session) -> list[AuthProvider]:
    """Enabled SSO providers, in display order, for the login page buttons."""
    return (
        db.query(AuthProvider)
        .filter(AuthProvider.is_enabled.is_(True))
        .order_by(AuthProvider.display_name)
        .all()
    )


@router.get("/login")
def show_login(
    request: Request,
    next: str = "/ui/dashboard",
    user: AppUser | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> Response:
    """Show the login form. If already logged in, redirect to the next page."""
    if user is not None:
        return RedirectResponse(url=next, status_code=303)
    return render(
        request, "login.html", providers=_enabled_providers(db), next=next
    )


@router.post("/login")
def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/ui/dashboard"),
    db: Session = Depends(get_db),
) -> Response:
    """Process login form submission."""
    user = db.query(AppUser).filter(AppUser.username == username).one_or_none()

    if (
        user is None
        or not user.is_active
        or user.password_hash is None
        or not verify_password(password, user.password_hash)
    ):
        log.info("ui_login_failed", extra={"username": username})
        record_event(
            category="auth",
            event_type="auth.login.failure",
            outcome="failure",
            actor_type="user",
            actor_label=username,
            message=f"Failed UI login for '{username}'",
            detail={"method": "password", "surface": "ui"},
            request=request,
        )
        return render(
            request,
            "login.html",
            error="Invalid username or password.",
            providers=_enabled_providers(db),
            next=next,
        )

    request.session[SESSION_USER_ID_KEY] = user.id
    request.session[SESSION_USERNAME_KEY] = user.username
    user.last_login_at = datetime.now(UTC)
    db.commit()

    log.info("ui_login_success", extra={"username": user.username, "user_id": user.id})
    record_event(
        category="auth",
        event_type="auth.login.success",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        message=f"{user.username} signed in",
        detail={"method": "password", "surface": "ui"},
        request=request,
    )
    flash(request, f"Welcome back, {user.username}.", "success")
    return RedirectResponse(url=next, status_code=303)


@router.post("/logout")
def do_logout(request: Request) -> Response:
    """Clear session and redirect to login."""
    user_id = request.session.get(SESSION_USER_ID_KEY)
    username = request.session.get(SESSION_USERNAME_KEY)
    request.session.clear()
    log.info("ui_logout", extra={"username": username})
    if username:
        record_event(
            category="auth",
            event_type="auth.logout",
            actor_type="user",
            actor_label=username,
            actor_id=user_id,
            message=f"{username} signed out",
            detail={"surface": "ui"},
            request=request,
        )
    return RedirectResponse(url="/ui/login", status_code=303)


@router.post("/theme")
def set_theme(
    theme: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Persist the logged-in user's UI color theme. Returns 204 (no body)."""
    user.theme = "dark" if theme == "dark" else "light"
    db.commit()
    return Response(status_code=204)


@router.get("/")
def ui_root() -> Response:
    """Redirect /ui to /ui/dashboard."""
    return RedirectResponse(url="/ui/dashboard", status_code=307)
