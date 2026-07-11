"""Jinja2 templates configuration and shared context helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.models import AppUser
from app.services.branding import current_branding
from app.services.system_config import tasks_enabled
from app.ui.flash import get_flashes

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _customer_logo_uri(customer: Any) -> str | None:
    """Jinja global: inline data URI for a customer's logo, or None.

    Lets any template render a customer logo without every route threading it
    through context. Safe on any object exposing ``.id`` (or None).
    """
    if customer is None:
        return None
    from app.services import customer_logo

    return customer_logo.data_uri(customer.id)


templates.env.globals["customer_logo_uri"] = _customer_logo_uri


def _asset_version() -> str:
    """A cache-busting token for /static assets, derived from the newest file
    mtime. Appended as ?v=... so browsers refetch app.css/app.js whenever they
    actually change (and not otherwise). Computed once at import."""
    try:
        mtimes = [p.stat().st_mtime_ns for p in _STATIC_DIR.glob("*") if p.is_file()]
        return str(max(mtimes)) if mtimes else "0"
    except OSError:
        return "0"


_ASSET_V = _asset_version()


def _setup_banner_flags(
    request: Request, current_user: AppUser | None
) -> dict[str, bool]:
    """Flags for the top-of-page setup banners (see base.html).

    - ``needs_email_setup``: the signed-in *local* user (has a password) has no
      email, so they can't self-serve a password reset.
    - ``smtp_setup_needed``: an admin is signed in, no SMTP is configured (so
      reset emails can't be sent), and they haven't dismissed the banner this
      session. Dismissal is cleared on login, so it reappears each sign-in.
    """
    needs_email_setup = bool(
        current_user is not None
        and current_user.password_hash is not None
        and not current_user.email
    )

    smtp_setup_needed = False
    if (
        current_user is not None
        and current_user.is_admin
        and not request.session.get("smtp_banner_dismissed")
    ):
        from app.db import get_session_factory
        from app.services import email as email_service

        db = get_session_factory()()
        try:
            smtp_setup_needed = not email_service.is_ready(db)
        finally:
            db.close()

    return {
        "needs_email_setup": needs_email_setup,
        "smtp_setup_needed": smtp_setup_needed,
    }


def render(
    request: Request,
    template_name: str,
    *,
    current_user: AppUser | None = None,
    **context: Any,
) -> Any:
    """Render a template with common context (current user, flashes, version, etc.).

    Always pull flashes into the context so the base layout can render them.
    """
    base_context: dict[str, Any] = {
        **_setup_banner_flags(request, current_user),
        "request": request,
        "current_user": current_user,
        "flashes": get_flashes(request),
        "app_version": __version__,
        "asset_v": _ASSET_V,
        "branding": current_branding(),
        "tasks_enabled": tasks_enabled(),
        "theme": getattr(current_user, "theme", None) or "light",
        "active_section": context.pop("active_section", None),
        "active_subsection": context.pop("active_subsection", None),
        "page_title": context.pop("page_title", None),
    }
    base_context.update(context)
    return templates.TemplateResponse(
        request=request, name=template_name, context=base_context
    )
