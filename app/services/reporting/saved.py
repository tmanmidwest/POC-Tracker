"""Saved-report persistence, ownership, and visibility resolution.

CRUD for :class:`app.models.SavedReport` plus the "who may see this report"
rules. Every definition is validated against the registry before it is written,
and visibility is resolved here (never in a template) so the list page and the
run route agree on exactly one answer.

Access is two independent gates, both of which must pass:

* **Capability** — ``report.view`` to run, ``report.create`` to author,
  ``report.publish`` to share/publish, ``report.schedule`` to schedule. Enforced
  at the route via ``require_capability``.
* **Visibility** — resolved here: ``private`` (owner only), ``shared`` (a role /
  region audience), ``published`` (any internal user). The *row set* is always
  re-scoped per viewer at run time by the engine — visibility only decides who
  may open the report, never which rows they get.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import AppUser, SavedReport
from app.models.saved_report import (
    AUDIENCE_CLIENT,
    AUDIENCE_INTERNAL,
    VALID_VISIBILITY,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLISHED,
    VISIBILITY_SHARED,
)
from app.services.regions import get_user_region_ids
from app.services.reporting import registry as reg


class ReportError(ValueError):
    """A saved-report operation was rejected (bad visibility, missing name, …)."""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_audience(visibility: str, audience: dict | None) -> dict | None:
    """Clean an audience blob to the shape its visibility allows.

    - ``shared``    -> ``{"role_keys": [...], "region_ids": [int, ...]}``
    - ``published`` -> ``{"kind": "client"|"internal"}``
    - ``private``   -> ``None``
    """
    if visibility == VISIBILITY_SHARED:
        audience = audience or {}
        role_keys = [str(r) for r in (audience.get("role_keys") or [])]
        region_ids = []
        for rid in audience.get("region_ids") or []:
            try:
                region_ids.append(int(rid))
            except (TypeError, ValueError):
                continue
        return {"role_keys": role_keys, "region_ids": region_ids}
    if visibility == VISIBILITY_PUBLISHED:
        kind = (audience or {}).get("kind")
        return {"kind": AUDIENCE_CLIENT if kind == AUDIENCE_CLIENT else AUDIENCE_INTERNAL}
    return None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_report(
    db: Session,
    owner: AppUser,
    *,
    name: str,
    description: str | None,
    entity: str,
    is_summary: bool,
    definition: dict,
    visibility: str = VISIBILITY_PRIVATE,
    audience: dict | None = None,
) -> SavedReport:
    """Validate + persist a new saved report owned by ``owner``."""
    name = (name or "").strip()
    if not name:
        raise ReportError("A report needs a name.")
    if visibility not in VALID_VISIBILITY:
        raise ReportError(f"Unknown visibility {visibility!r}.")
    # Raises DefinitionError (a ValueError) if anything is off-registry.
    clean = reg.validate_definition(entity, definition, summary=is_summary)

    report = SavedReport(
        owner_id=owner.id,
        name=name[:200],
        description=(description or "").strip()[:500] or None,
        entity=entity,
        is_summary=is_summary,
        definition=clean,
        visibility=visibility,
        audience=normalize_audience(visibility, audience),
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def update_report(
    db: Session,
    report: SavedReport,
    *,
    name: str,
    description: str | None,
    definition: dict,
    is_summary: bool,
    visibility: str,
    audience: dict | None,
) -> SavedReport:
    """Validate + apply edits to an existing report (entity is immutable)."""
    name = (name or "").strip()
    if not name:
        raise ReportError("A report needs a name.")
    if visibility not in VALID_VISIBILITY:
        raise ReportError(f"Unknown visibility {visibility!r}.")
    clean = reg.validate_definition(report.entity, definition, summary=is_summary)

    report.name = name[:200]
    report.description = (description or "").strip()[:500] or None
    report.is_summary = is_summary
    report.definition = clean
    report.visibility = visibility
    report.audience = normalize_audience(visibility, audience)
    db.commit()
    db.refresh(report)
    return report


def delete_report(db: Session, report: SavedReport) -> None:
    db.delete(report)
    db.commit()


def get_report(db: Session, report_id: int) -> SavedReport | None:
    return db.get(SavedReport, report_id)


def set_schedule(db: Session, report: SavedReport, schedule: dict | None) -> SavedReport:
    """Attach or clear a report's delivery schedule."""
    report.schedule = schedule
    db.commit()
    db.refresh(report)
    return report


# ---------------------------------------------------------------------------
# Visibility resolution
# ---------------------------------------------------------------------------


def can_edit_report(user: AppUser, report: SavedReport) -> bool:
    """Only the owner (or a superuser) may edit/delete/schedule a report."""
    if user.is_superuser:
        return True
    return report.owner_id is not None and report.owner_id == user.id


def can_view_report(db: Session, user: AppUser, report: SavedReport) -> bool:
    """Whether ``user`` may open/run ``report`` (independent of the capability gate).

    Owner and superusers always; ``published`` for any internal user; ``shared``
    when the user's role or one of their regions is in the audience.
    """
    if can_edit_report(user, report):
        return True
    if user.is_external:
        return False  # the report builder is internal-only in v1
    if report.visibility == VISIBILITY_PUBLISHED:
        return True
    if report.visibility == VISIBILITY_SHARED:
        aud = report.audience or {}
        if user.role in (aud.get("role_keys") or []):
            return True
        region_ids = set(aud.get("region_ids") or [])
        if region_ids and region_ids & get_user_region_ids(db, user.id):
            return True
    return False


def visible_reports(db: Session, user: AppUser) -> list[SavedReport]:
    """Reports ``user`` may see on the list page — own + shared-to-them + published.

    Superusers see every report. For everyone else we load the candidate set
    (own, published, and all shared) and filter shared entries through
    :func:`can_view_report`, since a shared audience lives in JSONB.
    """
    q = db.query(SavedReport)
    if not user.is_superuser:
        q = q.filter(
            or_(
                SavedReport.owner_id == user.id,
                SavedReport.visibility == VISIBILITY_PUBLISHED,
                SavedReport.visibility == VISIBILITY_SHARED,
            )
        )
    candidates = q.order_by(SavedReport.updated_at.desc()).all()
    return [r for r in candidates if can_view_report(db, user, r)]
