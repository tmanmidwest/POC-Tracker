"""Self-service account page for any logged-in user (/ui/profile).

Lets a user maintain their own display name and email, and change their own
password (with current-password confirmation). This is deliberately NOT under the
admin-only settings router — every signed-in user can manage their own account.

Email is editable for internal users; for external viewers it doubles as their
sign-in identity, so it's shown read-only. Password change is available to any
account with a local password (OIDC users have none).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.services import login_security
from app.services.audit import record_event
from app.services.passwords import hash_password, verify_password
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


def _normalize_optional_email(raw: str) -> str | None:
    """Strip + lowercase to match the invitation/admin flows; blank -> None so it
    stores as NULL rather than colliding on the unique constraint."""
    norm = (raw or "").strip().lower()
    return norm or None


@router.get("/profile")
def show_profile(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(request, "profile.html", current_user=user, target_user=user)


@router.post("/profile")
def update_profile(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    def _reject(message: str) -> Response:
        return render(
            request, "profile.html", current_user=user, target_user=user,
            error=message,
        )

    user.display_name = display_name.strip() or None

    # External viewers' email is their sign-in identity — not self-editable here.
    if not user.is_external:
        email_norm = _normalize_optional_email(email)
        if email_norm is not None and "@" not in email_norm:
            return _reject("Enter a valid email address, or leave it blank.")
        if email_norm is not None:
            clash = (
                db.query(AppUser)
                .filter(AppUser.email == email_norm, AppUser.id != user.id)
                .first()
            )
            if clash is not None:
                return _reject(f"The email '{email_norm}' is already in use.")
        user.email = email_norm

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _reject("That email is already in use.")

    record_event(
        category="account",
        event_type="account.profile_updated",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=user.id,
        target_label=user.username,
        message=f"{user.username} updated their profile",
        detail={"display_name": user.display_name, "email": user.email},
        request=request,
    )
    flash(request, "Your profile has been updated.", "success")
    return RedirectResponse(url="/ui/profile", status_code=303)


@router.post("/profile/password")
def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    def _reject(message: str) -> Response:
        return render(
            request, "profile.html", current_user=user, target_user=user,
            password_error=message,
        )

    if user.password_hash is None:
        # OIDC/SSO account with no local password to change.
        return _reject("This account signs in via SSO and has no password to change.")
    if not verify_password(current_password, user.password_hash):
        return _reject("Your current password is incorrect.")
    if new_password != confirm_password:
        return _reject("The new passwords don't match.")
    if len(new_password) < 8:
        return _reject("Password must be at least 8 characters.")

    user.password_hash = hash_password(new_password)
    # Changing your own password also lifts any lockout on the account.
    login_security.clear_lockout(user)
    db.commit()

    log.info("ui_self_password_changed", extra={"username": user.username})
    record_event(
        category="account",
        event_type="account.password_changed",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="app_user",
        target_id=user.id,
        target_label=user.username,
        message=f"{user.username} changed their own password",
        request=request,
    )
    flash(request, "Your password has been changed.", "success")
    return RedirectResponse(url="/ui/profile", status_code=303)
