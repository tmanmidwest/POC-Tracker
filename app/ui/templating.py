"""Jinja2 templates configuration and shared context helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import __version__
from app.models import AppUser
from app.services.branding import current_branding
from app.ui.flash import get_flashes

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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
        "request": request,
        "current_user": current_user,
        "flashes": get_flashes(request),
        "app_version": __version__,
        "branding": current_branding(),
        "active_section": context.pop("active_section", None),
        "active_subsection": context.pop("active_subsection", None),
        "page_title": context.pop("page_title", None),
    }
    base_context.update(context)
    return templates.TemplateResponse(
        request=request, name=template_name, context=base_context
    )
