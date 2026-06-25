"""Session-based authentication endpoints for the web UI.

These are JSON endpoints so they can be tested before any HTML UI exists.
The eventual HTML login form will POST to /login with form-encoded data;
that's a separate route added later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.schemas.auth import LoginRequest, LoginResponse, SessionInfoResponse
from app.services.audit import record_event
from app.services.auth import (
    SESSION_USER_ID_KEY,
    SESSION_USERNAME_KEY,
    get_optional_user,
)
from app.services.passwords import verify_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/session", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request,
    body: LoginRequest,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate with username and password. On success, sets a session cookie."""
    user = (
        db.query(AppUser)
        .filter(AppUser.username == body.username.lower())
        .one_or_none()
    )
    if user is None:
        # Also try case-insensitive match in case the seeded user was stored
        # with mixed case
        user = (
            db.query(AppUser).filter(AppUser.username == body.username).one_or_none()
        )

    auth_failed = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password.",
    )

    def _fail(reason: str) -> None:
        record_event(
            category="auth",
            event_type="auth.login.failure",
            outcome="failure",
            actor_type="user",
            actor_label=body.username,
            message=f"Failed API login for '{body.username}'",
            detail={"method": "password", "surface": "api", "reason": reason},
            request=request,
        )

    if user is None:
        log.info("login_failed", extra={"reason": "no_user", "username": body.username})
        _fail("no_user")
        raise auth_failed

    if not user.is_active:
        log.info(
            "login_failed", extra={"reason": "inactive", "username": body.username}
        )
        _fail("inactive")
        raise auth_failed

    if not verify_password(body.password, user.password_hash):
        log.info(
            "login_failed", extra={"reason": "bad_password", "username": body.username}
        )
        _fail("bad_password")
        raise auth_failed

    # Successful login
    request.session[SESSION_USER_ID_KEY] = user.id
    request.session[SESSION_USERNAME_KEY] = user.username
    user.last_login_at = datetime.now(UTC)
    db.commit()

    log.info("login_success", extra={"username": user.username, "user_id": user.id})
    record_event(
        category="auth",
        event_type="auth.login.success",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        message=f"{user.username} signed in (API)",
        detail={"method": "password", "surface": "api"},
        request=request,
    )
    return LoginResponse(username=user.username, user_id=user.id)


@router.post("/logout")
def logout(request: Request) -> dict[str, str]:
    """Clear the session cookie."""
    user_id = request.session.get(SESSION_USER_ID_KEY)
    username = request.session.get(SESSION_USERNAME_KEY)
    request.session.clear()
    log.info("logout", extra={"username": username})
    if username:
        record_event(
            category="auth",
            event_type="auth.logout",
            actor_type="user",
            actor_label=username,
            actor_id=user_id,
            message=f"{username} signed out (API)",
            detail={"surface": "api"},
            request=request,
        )
    return {"message": "Logged out."}


@router.get("/me", response_model=SessionInfoResponse)
def session_info(
    user: AppUser | None = Depends(get_optional_user),
) -> SessionInfoResponse:
    """Report whether the current session is authenticated."""
    if user is None:
        return SessionInfoResponse(authenticated=False)
    return SessionInfoResponse(
        authenticated=True, username=user.username, user_id=user.id
    )
