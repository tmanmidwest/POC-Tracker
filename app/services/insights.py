"""Derived portfolio signals shared by the dashboard and the project list.

These are *computed* qualities of a project (not stored columns), so keeping the
definitions in one place stops the dashboard KPIs and the project-list filters
from drifting apart: a project counted as "at risk" on the dashboard is exactly
the set you land on when you click that KPI card.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.models import Project

# No update in this many days → "stalled". Kept here as the single source of
# truth; the dashboard and project list both import it.
STALLED_DAYS = 14


def completed_use_cases(project: Project) -> int:
    return sum(
        1 for uc in project.use_cases if uc.status and uc.status.is_complete_status
    )


def completion_pct(project: Project) -> int:
    """Percentage of use cases in a complete status (0 when there are none)."""
    total = len(project.use_cases)
    return round(completed_use_cases(project) / total * 100) if total else 0


def is_at_risk(project: Project, today: date | None = None) -> bool:
    """Past its end date with incomplete use cases."""
    today = today or date.today()
    return (
        project.end_date is not None
        and project.end_date < today
        and completion_pct(project) < 100
    )


def idle_days(updated_at: datetime | None, now: datetime | None = None) -> int | None:
    """Whole days since a project was last touched, tolerant of naive/aware
    timestamps (SQLite stores naive UTC)."""
    if updated_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    u = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
    return (now - u).days


def is_stalled(project: Project, now: datetime | None = None) -> bool:
    """No update in ``STALLED_DAYS`` or more."""
    days = idle_days(project.updated_at, now)
    return days is not None and days >= STALLED_DAYS
