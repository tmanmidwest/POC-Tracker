"""Public (unauthenticated) password-reset pages.

A locked-out or forgetful local user requests a reset link, then opens the
emailed link to set a new password. These routes must be reachable while logged
out, so this router is mounted with no auth dependency. See
app.services.password_reset.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services import password_reset
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)


def _base_url(request: Request) -> str:
    """Externally-reachable base URL for building the reset link."""
    return get_settings().public_base_url or str(request.base_url).rstrip("/")


@router.get("/forgot-password")
def show_forgot(request: Request) -> Response:
    """Show the 'request a reset link' form."""
    return render(request, "forgot_password.html")


@router.post("/forgot-password")
def do_forgot(
    request: Request,
    identifier: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Email a reset link if the account exists. Always shows the same result."""
    password_reset.request_reset(
        db, identifier=identifier, base_url=_base_url(request), request=request
    )
    # Non-enumerating: the same confirmation regardless of whether we found an
    # account, whether it had an email, or whether SMTP delivery succeeded.
    return render(request, "forgot_password.html", sent=True)


@router.get("/reset/{token}")
def show_reset(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Show the set-new-password form for a valid token, or an 'invalid' page."""
    reset = password_reset.verify_token(db, token)
    if reset is None:
        return render(request, "reset_invalid.html")
    return render(request, "reset_password.html", token=token)


@router.post("/reset/{token}")
def do_reset(
    token: str,
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    """Set the new password, unlock the account, and send the user to sign in."""
    reset = password_reset.verify_token(db, token)
    if reset is None:
        return render(request, "reset_invalid.html")

    def _reject(message: str) -> Response:
        return render(request, "reset_password.html", token=token, error=message)

    if password != confirm:
        return _reject("The passwords don't match.")
    try:
        password_reset.consume(db, reset, new_password=password, request=request)
    except password_reset.PasswordResetError as exc:
        return _reject(str(exc))

    flash(request, "Your password has been reset. Please sign in.", "success")
    return RedirectResponse(url="/ui/login", status_code=303)
