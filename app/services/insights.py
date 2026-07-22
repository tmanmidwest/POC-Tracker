"""Derived portfolio signals shared by the dashboard and the project list.

These are *computed* qualities of a project (not stored columns), so keeping the
definitions in one place stops the dashboard KPIs and the project-list filters
from drifting apart: a project counted as "at risk" on the dashboard is exactly
the set you land on when you click that KPI card.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timezone

from app.models import Project, ProjectMilestone

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


# ---------------------------------------------------------------------------
# Milestone timeline health
# ---------------------------------------------------------------------------


def overdue_milestones(
    project: Project, today: date | None = None
) -> list[ProjectMilestone]:
    """Incomplete milestones whose target date has passed."""
    today = today or date.today()
    return [m for m in project.milestones if m.is_overdue(today)]


def is_off_track(project: Project, today: date | None = None) -> bool:
    """Has at least one overdue, incomplete milestone.

    Kept alongside ``is_at_risk``/``is_stalled`` so the dashboard KPI, the
    project-list filter, and the project-page chip all agree on the exact set.
    Terminal (closed) projects are never flagged — a finished POC isn't "off
    track" for a milestone it never closed out.
    """
    if is_closed(project):
        return False
    return bool(overdue_milestones(project, today))


def next_milestone(
    project: Project, today: date | None = None
) -> ProjectMilestone | None:
    """The upcoming (incomplete) milestone to focus on.

    Prefers the earliest dated one; falls back to the first undated incomplete
    milestone by timeline order. Returns None when everything is done.
    """
    incomplete = [m for m in project.milestones if not m.is_complete]
    if not incomplete:
        return None
    dated = [m for m in incomplete if m.target_date is not None]
    if dated:
        return min(dated, key=lambda m: m.target_date)  # type: ignore[arg-type,return-value]
    return min(incomplete, key=lambda m: m.sort_order)


def milestone_progress(project: Project) -> dict:
    """Simple done/total tally for the timeline header."""
    total = len(project.milestones)
    done = sum(1 for m in project.milestones if m.is_complete)
    return {"total": total, "done": done, "pct": round(done / total * 100) if total else 0}


# ---------------------------------------------------------------------------
# Win/loss outcome — derived from the project's status (single source of truth)
# ---------------------------------------------------------------------------


def outcome(project: Project) -> str:
    """The structured win/loss outcome of a project's current status.

    One of ``none`` | ``won`` | ``lost`` | ``no_decision``. In-flight projects
    (and any status not mapped to an outcome) are ``none``.
    """
    return project.status.outcome if project.status else "none"


def is_closed(project: Project) -> bool:
    """Reached a terminal status (won, lost, or otherwise decided)."""
    return bool(project.status and project.status.is_terminal)


def is_won(project: Project) -> bool:
    return outcome(project) == "won"


def is_lost(project: Project) -> bool:
    return outcome(project) == "lost"


def is_no_decision(project: Project) -> bool:
    return outcome(project) == "no_decision"


def is_open(project: Project) -> bool:
    """Still in flight — not in a terminal status."""
    return not is_closed(project)


def cycle_time_days(project: Project) -> int | None:
    """Days from start to close (``closed_date`` − ``start_date``).

    Returns ``None`` unless both dates are present. Uses ``closed_date`` (set on
    close), not ``updated_at``, so routine edits don't distort cycle time.
    """
    if project.start_date is None or project.closed_date is None:
        return None
    return (project.closed_date - project.start_date).days


def win_rate(won: int, lost: int) -> float | None:
    """Won / (won + lost) as a 0–100 percentage.

    ``no_decision`` deals are deliberately excluded from the denominator — a
    stalled deal that never chose is not a competitive loss. Returns ``None``
    when there are no decided deals (avoids a misleading 0%).
    """
    decided = won + lost
    return round(won / decided * 100, 1) if decided else None


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def portfolio_stats(
    projects: Iterable[Project], today: date | None = None
) -> dict:
    """Aggregate win/loss analytics over a set of projects.

    Pure over the passed iterable — the caller decides which projects are in
    scope (typically all projects, including archived closed ones, since closed
    deals are what win-rate is about). Everything here derives from
    ``status.outcome`` and the stored dates, so the dashboard and any report
    read the exact same numbers.
    """
    projects = list(projects)

    won = [p for p in projects if is_won(p)]
    lost = [p for p in projects if is_lost(p)]
    no_decision = [p for p in projects if is_no_decision(p)]
    open_ = [p for p in projects if is_open(p)]
    decided = len(won) + len(lost)

    cycle_all = [d for p in projects if (d := cycle_time_days(p)) is not None]
    cycle_won = [d for p in won if (d := cycle_time_days(p)) is not None]

    # Win rate by Sales Engineer (only SEs who have a decided deal appear).
    se_won: dict[str, int] = defaultdict(int)
    se_lost: dict[str, int] = defaultdict(int)
    for p in won:
        se_won[_se_name(p)] += 1
    for p in lost:
        se_lost[_se_name(p)] += 1
    by_sales_engineer = _breakdown_rows(se_won, se_lost)

    # Win rate by project type.
    type_won: dict[str, int] = defaultdict(int)
    type_lost: dict[str, int] = defaultdict(int)
    for p in won:
        type_won[_type_name(p)] += 1
    for p in lost:
        type_lost[_type_name(p)] += 1
    by_type = _breakdown_rows(type_won, type_lost)

    # Why we lose: reasons and competitors on lost deals.
    loss_reasons = _labeled_counts(
        p.close_reason.name if p.close_reason else "Unspecified" for p in lost
    )
    competitors = _labeled_counts(
        (p.competitor or "").strip() for p in lost if (p.competitor or "").strip()
    )

    return {
        "total": len(projects),
        "open": len(open_),
        "won": len(won),
        "lost": len(lost),
        "no_decision": len(no_decision),
        "decided": decided,
        "win_rate": win_rate(len(won), len(lost)),
        "avg_cycle_time_days": _avg(cycle_all),
        "avg_cycle_time_won_days": _avg(cycle_won),
        "by_sales_engineer": by_sales_engineer,
        "by_type": by_type,
        "loss_reasons": loss_reasons,
        "competitors": competitors,
    }


def _se_name(project: Project) -> str:
    se = project.sales_engineer
    if se is None:
        return "Unassigned"
    return se.display_label


def _type_name(project: Project) -> str:
    return project.type.name if project.type else "Untyped"


def _breakdown_rows(
    won_by: dict[str, int], lost_by: dict[str, int]
) -> list[dict]:
    """Merge won/lost counts per key into sorted rows with a win rate.

    Ordered by most decided deals first, so the busiest SE/type leads.
    """
    keys = set(won_by) | set(lost_by)
    rows = [
        {
            "label": k,
            "won": won_by.get(k, 0),
            "lost": lost_by.get(k, 0),
            "win_rate": win_rate(won_by.get(k, 0), lost_by.get(k, 0)),
        }
        for k in keys
    ]
    rows.sort(key=lambda r: (-(r["won"] + r["lost"]), r["label"].lower()))
    return rows


def _labeled_counts(labels: Iterable[str]) -> list[dict]:
    """Count occurrences, returned as rows sorted most-frequent first."""
    counts = Counter(labels)
    rows = [{"label": k, "count": v} for k, v in counts.items()]
    rows.sort(key=lambda r: (-r["count"], r["label"].lower()))
    return rows
