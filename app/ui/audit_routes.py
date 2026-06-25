"""Activity / audit log UI — view, filter, and export recorded events."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.models.audit_event import AuditEvent
from app.services import system_config
from app.ui.dependencies import require_ui_user
from app.ui.templating import render

router = APIRouter(prefix="/ui/activity", tags=["ui"], include_in_schema=False)

# Specific categories selectable in the dropdown.
CATEGORIES: list[str] = [
    "auth",
    "oauth",
    "oidc",
    "customer",
    "project",
    "library",
    "lookup",
    "api_key",
    "oauth_client",
    "auth_provider",
    "branding",
    "admin_user",
    "system",
]
OUTCOMES: list[str] = ["success", "failure", "error"]

# Quick-filter "views" that group several categories. Lets a user pick, in one
# click, "just the identity & OAuth logs" — the headline use case.
VIEWS: dict[str, dict[str, Any]] = {
    "identity": {"label": "Identity & OAuth", "categories": ["auth", "oauth", "oidc"]},
    "data": {"label": "Data changes", "categories": ["customer", "project", "library", "lookup"]},
    "admin": {
        "label": "Admin & config",
        "categories": [
            "api_key",
            "oauth_client",
            "auth_provider",
            "branding",
            "admin_user",
            "system",
        ],
    },
}

DEFAULT_LIMIT = 100
LIMIT_CHOICES = [100, 250, 500, 1000]
EXPORT_CAP = 50_000  # safety cap on rows in a single export


def _parse_date(value: str | None) -> datetime | None:
    """Parse a date or datetime-local string into a UTC datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _filtered_query(
    *,
    view: str | None,
    category: str | None,
    outcome: str | None,
    event_type: str | None,
    actor: str | None,
    q: str | None,
    date_from: str | None,
    date_to: str | None,
) -> Select[tuple[AuditEvent]]:
    """Build the ordered, filtered SELECT shared by the list and export views."""
    stmt = select(AuditEvent)

    if category:
        stmt = stmt.where(AuditEvent.category == category)
    elif view and view in VIEWS:
        stmt = stmt.where(AuditEvent.category.in_(VIEWS[view]["categories"]))

    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type.like(f"%{event_type}%"))
    if actor:
        stmt = stmt.where(AuditEvent.actor_label.like(f"%{actor}%"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                AuditEvent.message.like(like),
                AuditEvent.event_type.like(like),
                AuditEvent.actor_label.like(like),
                AuditEvent.target_label.like(like),
                AuditEvent.detail_json.like(like),
            )
        )

    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    if parsed_from:
        stmt = stmt.where(AuditEvent.occurred_at >= parsed_from)
    if parsed_to:
        stmt = stmt.where(AuditEvent.occurred_at <= parsed_to)

    return stmt.order_by(AuditEvent.occurred_at.desc())


def _export_query_string(filters: dict[str, str]) -> str:
    """Build a query string of the active filters (for the export links)."""
    parts = [f"{k}={v}" for k, v in filters.items() if v]
    return "&".join(parts)


@router.get("")
def list_events(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    view: str | None = Query(default=None),
    category: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT),
) -> Response:
    """Render the Activity page with the current filters applied."""
    if limit not in LIMIT_CHOICES:
        limit = DEFAULT_LIMIT

    stmt = _filtered_query(
        view=view,
        category=category,
        outcome=outcome,
        event_type=event_type,
        actor=actor,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    events = list(db.scalars(stmt.limit(limit)))

    filters = {
        "view": view or "",
        "category": category or "",
        "outcome": outcome or "",
        "event_type": event_type or "",
        "actor": actor or "",
        "q": q or "",
        "date_from": date_from or "",
        "date_to": date_to or "",
    }

    return render(
        request,
        "audit/list.html",
        current_user=user,
        active_section="activity",
        page_title="Activity",
        events=events,
        total=total,
        shown=len(events),
        limit=limit,
        filters=filters,
        categories=CATEGORIES,
        outcomes=OUTCOMES,
        views=VIEWS,
        limit_choices=LIMIT_CHOICES,
        export_qs=_export_query_string(filters),
        retention_days=system_config.current_retention_days(),
    )


def _export_rows(
    db: Session,
    *,
    view: str | None,
    category: str | None,
    outcome: str | None,
    event_type: str | None,
    actor: str | None,
    q: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list[AuditEvent]:
    stmt = _filtered_query(
        view=view,
        category=category,
        outcome=outcome,
        event_type=event_type,
        actor=actor,
        q=q,
        date_from=date_from,
        date_to=date_to,
    ).limit(EXPORT_CAP)
    return list(db.scalars(stmt))


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


@router.get("/export.json")
def export_json(
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    view: str | None = Query(default=None),
    category: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> Response:
    """Download the filtered events as a JSON array (newest first)."""
    rows = _export_rows(
        db,
        view=view,
        category=category,
        outcome=outcome,
        event_type=event_type,
        actor=actor,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )
    payload = json.dumps([e.to_dict() for e in rows], indent=2, default=str)
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="activity-{_timestamp()}.json"'
        },
    )


@router.get("/export.csv")
def export_csv(
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
    view: str | None = Query(default=None),
    category: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> Response:
    """Download the filtered events as CSV (detail flattened to a JSON column)."""
    rows = _export_rows(
        db,
        view=view,
        category=category,
        outcome=outcome,
        event_type=event_type,
        actor=actor,
        q=q,
        date_from=date_from,
        date_to=date_to,
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "occurred_at",
            "category",
            "event_type",
            "outcome",
            "actor_type",
            "actor_label",
            "target_type",
            "target_id",
            "target_label",
            "message",
            "ip_address",
            "request_id",
            "detail_json",
        ]
    )
    for e in rows:
        writer.writerow(
            [
                e.occurred_at.isoformat() if e.occurred_at else "",
                e.category,
                e.event_type,
                e.outcome,
                e.actor_type,
                e.actor_label or "",
                e.target_type or "",
                e.target_id or "",
                e.target_label or "",
                e.message,
                e.ip_address or "",
                e.request_id or "",
                json.dumps(e.detail, default=str) if e.detail else "",
            ]
        )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="activity-{_timestamp()}.csv"'
        },
    )
