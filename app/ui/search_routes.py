"""Global search UI: an as-you-type suggestion dropdown and a full results page.

Open to any logged-in user; results only ever include domain content (never
admin-only settings).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.services import search as search_service
from app.services.scope import get_scope, resolve_scope, scoped_project_ids
from app.ui.dependencies import require_ui_user
from app.ui.templating import render

router = APIRouter(prefix="/ui/search", tags=["ui"], include_in_schema=False)


@router.get("/suggest")
def suggest(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """HTMX fragment: top hits grouped by type for the live dropdown."""
    # The dropdown has no toggle of its own, so honour the user's stored scope.
    scope = get_scope(db, user)
    groups = search_service.search(
        db, q, per_type_limit=5, overall_cap=30,
        visible_project_ids=scoped_project_ids(db, user, scope),
        restrict_unscoped=user.is_external,
    )
    return render(
        request,
        "search/_suggest.html",
        current_user=user,
        query=q,
        groups=groups,
        total=search_service.total_hits(groups),
    )


@router.get("")
def results(
    request: Request,
    q: str = "",
    scope: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Full results page, grouped by entity type."""
    scope = resolve_scope(db, user, scope)
    groups = search_service.search(
        db, q, per_type_limit=20, overall_cap=120,
        visible_project_ids=scoped_project_ids(db, user, scope),
        restrict_unscoped=user.is_external,
    )
    return render(
        request,
        "search/results.html",
        current_user=user,
        active_section="search",
        query=q,
        groups=groups,
        total=search_service.total_hits(groups),
        scope=scope,
    )
