"""Milestone application — seeding a POC's timeline from a blueprint.

New POCs get their lifecycle milestones from one of two blueprints, in order of
precedence:
  1. the POC template they were created from, if it carries milestones;
  2. otherwise the global default set (``milestone_defaults``).

Both store dates as day-offsets from the project start, re-anchored here to real
dates. If the project has no start date, milestones are seeded undated (they
still show on the timeline; they just never count as overdue).

Health/derived signals (overdue, off-track, next milestone) live in
``app.services.insights`` alongside the other portfolio signals.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MilestoneDefault,
    PocTemplate,
    Project,
    ProjectMilestone,
)


def _target(base: date | None, offset: int | None) -> date | None:
    if base is None or offset is None:
        return None
    return base + timedelta(days=offset)


def default_milestone_blueprints(db: Session) -> list[MilestoneDefault]:
    """Active default-set rows, in timeline order."""
    return list(
        db.scalars(
            select(MilestoneDefault)
            .where(MilestoneDefault.is_active.is_(True))
            .order_by(MilestoneDefault.sort_order, MilestoneDefault.name)
        ).all()
    )


def build_from_defaults(db: Session, base_date: date | None) -> list[ProjectMilestone]:
    """New (unattached) ProjectMilestone rows from the global default set."""
    return [
        ProjectMilestone(
            name=b.name,
            target_date=_target(base_date, b.target_offset_days),
            sort_order=b.sort_order,
        )
        for b in default_milestone_blueprints(db)
    ]


def build_from_template(
    template: PocTemplate, base_date: date | None
) -> list[ProjectMilestone]:
    """New (unattached) ProjectMilestone rows from a template's milestones."""
    return [
        ProjectMilestone(
            name=m.name,
            target_date=_target(base_date, m.target_offset_days),
            sort_order=m.sort_order,
        )
        for m in template.milestones
    ]


def seed_project_milestones(
    db: Session,
    project: Project,
    *,
    template: PocTemplate | None = None,
    base_date: date | None = None,
) -> int:
    """Attach starter milestones to a freshly created project.

    Uses the template's milestones when the template has any, else the global
    default set. No-ops if the project already has milestones (so re-saving a
    project never duplicates them). Caller commits.
    """
    if project.milestones:
        return 0
    base = base_date if base_date is not None else project.start_date
    rows: list[ProjectMilestone]
    if template is not None and template.milestones:
        rows = build_from_template(template, base)
    else:
        rows = build_from_defaults(db, base)
    for r in rows:
        project.milestones.append(r)
    return len(rows)


def next_sort_order(project: Project) -> int:
    """Sort order to append a new milestone at the end of the timeline."""
    return max((m.sort_order for m in project.milestones), default=0) + 10


def set_complete(milestone: ProjectMilestone, complete: bool) -> None:
    """Mark a milestone done (stamping today) or reopen it."""
    if complete and milestone.completed_date is None:
        milestone.completed_date = date.today()
    elif not complete:
        milestone.completed_date = None
