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
