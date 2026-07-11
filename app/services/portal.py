"""Customer portal service: public share-link lifecycle + customer-safe view.

This backs the no-login ``/portal/<token>`` status page. Two responsibilities:

1. **Link lifecycle** — create / fetch / enable / disable / rotate the per-project
   :class:`ProjectShareLink`, and record views.
2. **Customer-safe projection** — turn a project into exactly the data a customer
   should see: overall progress, use cases grouped by category with friendly
   pass/pending/blocked states, and the *non-internal* journal notes. Internal
   notes, tasks, and internal links never pass through here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from itertools import groupby
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Project, ProjectShareLink
from app.models.project_share_link import generate_token


# --------------------------------------------------------------------------- #
# Link lifecycle
# --------------------------------------------------------------------------- #
def get_link(db: Session, project_id: int) -> ProjectShareLink | None:
    """The share link for a project, or None if never created."""
    return (
        db.query(ProjectShareLink)
        .filter(ProjectShareLink.project_id == project_id)
        .one_or_none()
    )


def get_or_create_link(
    db: Session, project: Project, *, created_by: str | None
) -> ProjectShareLink:
    """Fetch the project's share link, creating (enabled) one if absent."""
    link = get_link(db, project.id)
    if link is None:
        link = ProjectShareLink(
            project_id=project.id,
            token=generate_token(),
            is_enabled=True,
            created_by=created_by,
        )
        db.add(link)
        db.flush()
    return link


def set_enabled(db: Session, link: ProjectShareLink, enabled: bool) -> None:
    link.is_enabled = enabled


def rotate_token(db: Session, link: ProjectShareLink) -> str:
    """Mint a fresh token (dead-links the old URL). Returns the new token."""
    link.token = generate_token()
    return link.token


def resolve_public(db: Session, token: str) -> ProjectShareLink | None:
    """A live, viewable link for ``token``, or None.

    Returns None for unknown/disabled tokens and archived projects, so the route
    can 404 uniformly (no distinction that would let a URL be probed).
    """
    if not token:
        return None
    link = (
        db.query(ProjectShareLink)
        .filter(ProjectShareLink.token == token)
        .one_or_none()
    )
    if link is None or not link.is_enabled:
        return None
    if link.project is None or link.project.is_archived:
        return None
    return link


def record_view(db: Session, link: ProjectShareLink) -> None:
    """Increment view telemetry. Uses a SQL-side increment to avoid races."""
    db.query(ProjectShareLink).filter(ProjectShareLink.id == link.id).update(
        {
            ProjectShareLink.view_count: ProjectShareLink.view_count + 1,
            ProjectShareLink.last_viewed_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )


# --------------------------------------------------------------------------- #
# Customer-safe projection
# --------------------------------------------------------------------------- #
def _uc_state(use_case: Any) -> str:
    """Map a use-case status to a coarse customer-facing state.

    States: ``passed`` · ``in_progress`` · ``blocked`` · ``na`` · ``pending``.
    """
    status = use_case.status
    if status is None:
        return "pending"
    if status.is_complete_status:
        return "passed"
    name = (status.name or "").lower()
    if "block" in name:
        return "blocked"
    if "not applicable" in name or name == "n/a":
        return "na"
    if "progress" in name:
        return "in_progress"
    return "pending"


# Human labels for each coarse state, shown on the customer page.
STATE_LABELS: dict[str, str] = {
    "passed": "Validated",
    "in_progress": "In progress",
    "blocked": "Blocked",
    "na": "Not applicable",
    "pending": "Planned",
}


def _progress(project: Project) -> dict[str, int]:
    total = len(project.use_cases)
    done = sum(
        1 for uc in project.use_cases if uc.status and uc.status.is_complete_status
    )
    return {"total": total, "done": done, "pct": round(done / total * 100) if total else 0}


def _grouped(project: Project) -> list[dict[str, Any]]:
    """Use cases grouped by category, each annotated with a friendly state."""
    def ref_key(u: Any) -> tuple:
        raw = (u.reference_number or "").split(".")
        return tuple(int(p) if p.isdigit() else 0 for p in raw)

    ucs = sorted(
        project.use_cases,
        key=lambda u: (u.category.lower(), ref_key(u), u.name.lower()),
    )
    groups: list[dict[str, Any]] = []
    for category, items in groupby(ucs, key=lambda u: u.category):
        rows = []
        for uc in items:
            state = _uc_state(uc)
            rows.append(
                {
                    "ref": uc.reference_number,
                    "name": uc.name,
                    "description": uc.description,
                    "feature": uc.feature_type.name if uc.feature_type else None,
                    "completed_on": uc.completed_on,
                    "state": state,
                    "state_label": STATE_LABELS[state],
                }
            )
        groups.append({"category": category, "use_cases": rows})
    return groups


def public_context(project: Project) -> dict[str, Any]:
    """Everything the customer page needs — and nothing it shouldn't have.

    Only non-internal notes are included; internal-only notes, tasks, and
    internal links (Salesforce/notebook/instance) are deliberately excluded.
    """
    notes = [
        n for n in sorted(project.note_entries, key=lambda n: n.note_date, reverse=True)
        if not n.is_internal_only
    ]
    last_update = notes[0].note_date if notes else None

    days_remaining: int | None = None
    if project.end_date and not (project.status and project.status.is_terminal):
        days_remaining = (project.end_date - date.today()).days

    return {
        "project": project,
        "progress": _progress(project),
        "use_case_groups": _grouped(project),
        "notes": notes,
        "last_update": last_update,
        "days_remaining": days_remaining,
    }
