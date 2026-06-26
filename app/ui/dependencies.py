"""UI-specific auth dependencies.

The JSON `get_current_user` raises 401 — fine for JSON endpoints, awful for
HTML routes. `require_ui_user` raises a special exception that the app catches
and turns into a redirect to /ui/login?next=<original-url>.

`require_admin_ui` additionally enforces the Admin group: standard users are
bounced to the dashboard with a flash rather than seeing admin-only surfaces
(lookups, the use-case library manager, and settings).
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import get_db
from app.models import AppUser
from app.services.auth import SESSION_USER_ID_KEY
from app.ui.flash import flash


class _RedirectToLogin(StarletteHTTPException):
    """Sentinel exception that an exception handler turns into a redirect."""

    def __init__(self, next_url: str) -> None:
        super().__init__(status_code=302, detail="login required")
        self.next_url = next_url


class _Forbidden(StarletteHTTPException):
    """Sentinel for a logged-in but non-admin user hitting an admin route."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail="admins only")


def require_ui_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """Return the logged-in AppUser, or redirect to /ui/login on failure."""
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if not user_id:
        raise _RedirectToLogin(next_url=str(request.url.path))
    user = db.get(AppUser, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise _RedirectToLogin(next_url=str(request.url.path))
    return user


def require_admin_ui(
    request: Request, user: AppUser = Depends(require_ui_user)
) -> AppUser:
    """Like require_ui_user but also requires the Admin group."""
    if not user.is_admin:
        raise _Forbidden()
    return user


def require_internal_ui(
    request: Request, user: AppUser = Depends(require_ui_user)
) -> AppUser:
    """Like require_ui_user but rejects external (read-only) viewers.

    Gates every mutating UI route so external viewers cannot create, edit, or
    delete anything even by POSTing directly.
    """
    if user.is_external:
        raise _Forbidden()
    return user


def redirect_to_login_handler(
    _request: Request, exc: _RedirectToLogin
) -> RedirectResponse:
    next_param = quote(exc.next_url, safe="/")
    return RedirectResponse(url=f"/ui/login?next={next_param}", status_code=303)


def forbidden_handler(request: Request, _exc: _Forbidden) -> RedirectResponse:
    flash(request, "That area is restricted to administrators.", "error")
    return RedirectResponse(url="/ui/dashboard", status_code=303)


# Re-exports for main.py
RedirectToLogin = _RedirectToLogin
Forbidden = _Forbidden
