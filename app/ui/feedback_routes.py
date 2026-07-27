"""HTML UI for user feedback (bug reports & feature requests).

Three surfaces:

* **Submit** (any signed-in user): a short form plus a list of the submissions
  the current user has made, with their current status.
* **Browse** (any internal user): a read-only board of *all* feedback grouped by
  status, so internal users can see what's been raised and where it is in the
  process — without the drag-to-move, priority, internal notes, or delete
  controls the admin board carries. Never exposes ``admin_notes``.
* **Manage** (admins only): a Kanban board grouped by status with drag-to-move,
  plus a per-item detail page to set priority, edit status, and keep internal
  notes.

Statuses are an admin-managed global lookup (see ``feedback-statuses`` in
``lookup_routes``). Mirrors the Task Manager patterns (PRG redirects, flashes,
audit events).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Feedback, FeedbackStatus
from app.models.feedback import (
    FEEDBACK_KIND_LABELS,
    FEEDBACK_KINDS,
    FEEDBACK_PRIORITIES,
)
from app.services.audit import record_event
from app.ui.dependencies import require_capability, require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/feedback", tags=["ui"], include_in_schema=False)

# The feedback board (/manage) requires the feedback.manage capability. While the
# dynamic-RBAC switch is off this is identical to the old admins-only gate.
require_feedback_manage = require_capability("feedback.manage")

# The read-only browse board (/all) requires feedback.view — an internal-tier
# capability, so while the dynamic-RBAC switch is off every internal user (and
# admins) can reach it, but external viewers cannot.
require_feedback_view = require_capability("feedback.view")


def _feedback_event(
    request: Request, user: AppUser, item: Feedback, event: str, verb: str
) -> None:
    record_event(
        category="feedback",
        event_type=f"feedback.{event}",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="feedback",
        target_id=item.id,
        target_label=item.title,
        message=f"{verb} feedback '{item.title}'",
        detail={"surface": "ui"},
        request=request,
    )


def _default_status_id(db: Session) -> int | None:
    """The lowest-sort active status new submissions land in (e.g. 'New')."""
    status = (
        db.query(FeedbackStatus)
        .filter(FeedbackStatus.is_active.is_(True))
        .order_by(FeedbackStatus.sort_order)
        .first()
    )
    return status.id if status else None


# ---------------------------------------------------------------------------
# Submit (any signed-in user)
# ---------------------------------------------------------------------------


@router.get("")
@router.get("/")
def submit_page(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """The submission form plus the current user's own submissions."""
    mine = (
        db.query(Feedback)
        .filter(Feedback.submitter_user_id == user.id)
        .order_by(Feedback.created_at.desc())
        .all()
    )
    return render(
        request,
        "feedback/submit.html",
        current_user=user,
        active_section="feedback",
        mine=mine,
        kind_labels=FEEDBACK_KIND_LABELS,
        form={"kind": "bug"},
    )


@router.post("")
@router.post("/")
async def create_feedback(
    request: Request,
    kind: str = Form(...),
    title: str = Form(...),
    body: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    title = (title or "").strip()
    kind = kind if kind in FEEDBACK_KINDS else "bug"
    if not title:
        flash(request, "A short title is required.", "error")
        return RedirectResponse(url="/ui/feedback", status_code=303)

    status_id = _default_status_id(db)
    if status_id is None:
        flash(
            request,
            "Feedback can't be submitted right now — no statuses are configured.",
            "error",
        )
        return RedirectResponse(url="/ui/feedback", status_code=303)

    item = Feedback(
        submitter_user_id=user.id,
        submitter_label=user.display_label,
        kind=kind,
        title=title[:300],
        body=(body or "").strip() or None,
        status_id=status_id,
    )
    db.add(item)
    db.commit()
    _feedback_event(request, user, item, "created", "Submitted")
    flash(request, "Thanks! Your feedback was submitted.", "success")
    return RedirectResponse(url="/ui/feedback", status_code=303)


# ---------------------------------------------------------------------------
# Browse (read-only — any internal user)
# ---------------------------------------------------------------------------


def _board_columns(
    db: Session, kind: str | None
) -> tuple[list[dict[str, Any]], list[Feedback], list[Feedback]]:
    """Group all feedback into per-status columns (shared by browse + manage).

    Returns ``(columns, orphans, items)`` where ``columns`` are the statuses in
    sort order each with their cards, ``orphans`` are items on a status that no
    longer exists, and ``items`` is the flat filtered list.
    """
    statuses = db.query(FeedbackStatus).order_by(FeedbackStatus.sort_order).all()

    kind_filter = kind if kind in FEEDBACK_KINDS else None
    base = db.query(Feedback)
    if kind_filter:
        base = base.filter(Feedback.kind == kind_filter)
    items = base.order_by(Feedback.created_at.desc()).all()

    by_status: dict[int, list[Feedback]] = {s.id: [] for s in statuses}
    orphans: list[Feedback] = []
    for it in items:
        by_status.get(it.status_id, orphans).append(it)

    columns = [{"status": s, "cards": by_status[s.id]} for s in statuses]
    return columns, orphans, items


@router.get("/all")
def browse_board(
    request: Request,
    kind: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_view),
) -> Response:
    """Read-only board of all feedback, so internal users can see where it is.

    Same grouping as the manage board but with no controls and no ``admin_notes``.
    The template renders a board (default) and a list; a client-side toggle
    switches between them.
    """
    columns, orphans, items = _board_columns(db, kind)
    kind_filter = kind if kind in FEEDBACK_KINDS else None
    return render(
        request,
        "feedback/browse.html",
        current_user=user,
        active_section="feedback_all",
        columns=columns,
        orphans=orphans,
        items=items,
        total=len(items),
        kind_filter=kind_filter,
        kind_labels=FEEDBACK_KIND_LABELS,
    )


# ---------------------------------------------------------------------------
# Manage (admins only)
# ---------------------------------------------------------------------------


@router.get("/manage")
def manage_board(
    request: Request,
    kind: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_manage),
) -> Response:
    """Kanban board of all feedback, grouped by status (columns in sort order)."""
    columns, orphans, items = _board_columns(db, kind)
    kind_filter = kind if kind in FEEDBACK_KINDS else None
    return render(
        request,
        "feedback/board.html",
        current_user=user,
        active_section="feedback_manage",
        columns=columns,
        orphans=orphans,
        total=len(items),
        kind_filter=kind_filter,
        kind_labels=FEEDBACK_KIND_LABELS,
    )


def _get_item(db: Session, fid: int) -> Feedback:
    item = db.get(Feedback, fid)
    if item is None:
        raise HTTPException(status_code=404, detail="Feedback not found.")
    return item


@router.get("/manage/{fid}")
def manage_detail(
    fid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_manage),
) -> Response:
    item = _get_item(db, fid)
    statuses = db.query(FeedbackStatus).order_by(FeedbackStatus.sort_order).all()
    return render(
        request,
        "feedback/detail.html",
        current_user=user,
        active_section="feedback_manage",
        item=item,
        statuses=statuses,
        priorities=FEEDBACK_PRIORITIES,
        kind_labels=FEEDBACK_KIND_LABELS,
    )


@router.post("/manage/{fid}")
async def update_feedback(
    fid: int,
    request: Request,
    status_id: int = Form(...),
    priority: str = Form(""),
    admin_notes: str = Form(""),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_manage),
) -> Response:
    item = _get_item(db, fid)
    if db.get(FeedbackStatus, status_id) is None:
        flash(request, "Unknown status.", "error")
        return RedirectResponse(url=f"/ui/feedback/manage/{fid}", status_code=303)

    item.status_id = status_id
    item.priority = priority if priority in FEEDBACK_PRIORITIES else None
    item.admin_notes = (admin_notes or "").strip() or None
    db.commit()
    _feedback_event(request, user, item, "updated", "Updated")
    flash(request, "Feedback updated.", "success")
    return RedirectResponse(url="/ui/feedback/manage", status_code=303)


@router.post("/manage/{fid}/status")
def set_feedback_status(
    fid: int,
    request: Request,
    status_id: int = Form(...),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_manage),
) -> Response:
    """Quick status change — used by the board's drag-and-drop."""
    item = _get_item(db, fid)
    if db.get(FeedbackStatus, status_id) is None:
        raise HTTPException(status_code=400, detail="Unknown status.")
    item.status_id = status_id
    db.commit()
    _feedback_event(request, user, item, "status_changed", "Changed status of")
    return Response(status_code=204)


@router.post("/manage/{fid}/delete")
def delete_feedback(
    fid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_feedback_manage),
) -> Response:
    item = _get_item(db, fid)
    title = item.title
    snapshot_id = item.id
    db.delete(item)
    db.commit()
    record_event(
        category="feedback",
        event_type="feedback.deleted",
        actor_type="user",
        actor_label=user.username,
        actor_id=user.id,
        target_type="feedback",
        target_id=snapshot_id,
        target_label=title,
        message=f"Deleted feedback '{title}'",
        detail={"surface": "ui"},
        request=request,
    )
    flash(request, f"Feedback '{title}' deleted.", "success")
    return RedirectResponse(url="/ui/feedback/manage", status_code=303)
