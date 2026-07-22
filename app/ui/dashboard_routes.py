"""HTML UI dashboard — projects grouped by status, with per-user view prefs."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db import get_db
from app.models import AppUser, DashboardPref, Project, ProjectStatus
from app.services.insights import (
    STALLED_DAYS,
    idle_days,
    is_at_risk,
    is_stalled,
    overdue_milestones,
)
from app.services.scope import (
    get_scope,
    resolve_scope,
    scoped_project_ids,
    selectable_engineers,
)
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/dashboard", tags=["ui"], include_in_schema=False)

# Insight thresholds — deliberately simple and tunable. STALLED_DAYS lives in
# app.services.insights (shared with the project-list filter).
EXPIRY_SOON_DAYS = 30  # external account expiring within this window → flagged
SE_LOAD_TOP = 8  # cap the "portfolio by engineer" chart to the busiest N
ATTENTION_LIMIT = 12  # most-urgent items surfaced in the attention panel


# All optional columns the user can toggle on the dashboard cards.
ALL_COLUMNS = [
    {"key": "name", "label": "Project"},
    {"key": "sales_engineer", "label": "Sales Engineer"},
    {"key": "account_executive", "label": "Account Exec"},
    {"key": "salesforce", "label": "Salesforce Opp"},
    {"key": "notebook", "label": "Notebook Link"},
    {"key": "poc_instance", "label": "POC Instance"},
    {"key": "start_date", "label": "Start"},
    {"key": "end_date", "label": "End"},
    {"key": "progress", "label": "Use-case progress"},
]
DEFAULT_COLUMNS = ["name", "sales_engineer", "salesforce", "notebook", "poc_instance", "start_date", "end_date", "progress"]
DEFAULT_SORT = "updated"  # updated | start_date | name


def _load_prefs(db: Session, user: AppUser) -> dict[str, Any]:
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    prefs: dict[str, Any] = {
        "columns": DEFAULT_COLUMNS,
        "status_ids": None,  # None = show all
        "status_order": None,  # None = use ProjectStatus.sort_order
        "sort": DEFAULT_SORT,
    }
    if row and row.config_json:
        try:
            stored = json.loads(row.config_json)
            prefs.update({k: v for k, v in stored.items() if v is not None})
        except (ValueError, TypeError):
            log.warning("dashboard_prefs_parse_failed", extra={"user": user.username})
    return prefs


def _order_statuses(
    statuses: list[ProjectStatus], order: list[int] | None
) -> list[ProjectStatus]:
    """Order statuses by the user's saved order; any not listed fall to the end
    keeping their canonical sort_order."""
    if not order:
        return statuses
    pos = {sid: i for i, sid in enumerate(order)}
    return sorted(statuses, key=lambda s: (pos.get(s.id, len(order)), s.sort_order))


def _progress(project: Project) -> dict[str, int]:
    total = len(project.use_cases)
    done = sum(
        1 for uc in project.use_cases if uc.status and uc.status.is_complete_status
    )
    pct = round(done / total * 100) if total else 0
    return {"total": total, "done": done, "pct": pct}


def _build_insights(
    db: Session,
    user: AppUser,
    visible_ids: set[int] | None,
    statuses: list[ProjectStatus],
) -> dict[str, Any]:
    """Portfolio-level aggregates for the dashboard insight strip.

    Computed over the same scoped set of active projects the tables use, so the
    KPIs and charts always agree with the "My / All / <engineer>" filter. Returns
    plain dicts/lists ready to hand to the template (and to JSON for the charts).
    """
    q = (
        db.query(Project)
        .options(
            joinedload(Project.customer),
            joinedload(Project.sales_engineer),
            selectinload(Project.use_cases),
            selectinload(Project.milestones),
        )
        .filter(Project.is_archived.is_(False))
    )
    if visible_ids is not None:
        q = q.filter(Project.id.in_(visible_ids))
    projects = q.all()

    today = date.today()
    now = datetime.now(timezone.utc)
    status_name = {s.id: s.name for s in statuses}

    pcts: list[int] = []
    at_risk = 0
    stalled = 0
    off_track = 0
    status_counter: Counter[int] = Counter()
    type_counter: Counter[str] = Counter()
    se_counter: Counter[str] = Counter()
    uc_status_counter: Counter[str] = Counter()
    feature_counter: Counter[str] = Counter()
    attention: list[dict[str, Any]] = []

    for p in projects:
        prog = _progress(p)
        if prog["total"]:
            pcts.append(prog["pct"])
        status_counter[p.status_id] += 1
        type_counter[p.type.name if p.type else "Untyped"] += 1
        eng = p.sales_engineer.display_label if p.sales_engineer else "Unassigned"
        se_counter[eng] += 1
        for uc in p.use_cases:
            uc_status_counter[uc.status.name if uc.status else "—"] += 1
            feature_counter[uc.feature_type.name if uc.feature_type else "Uncategorized"] += 1

        reasons: list[dict[str, str]] = []
        severity = 0  # higher = more urgent, drives sort order
        days = 0
        if is_at_risk(p, today):
            at_risk += 1
            overdue = (today - p.end_date).days
            reasons.append({"kind": "overdue", "label": f"{overdue}d past end date"})
            severity = max(severity, 2)
            days = max(days, overdue)
        if is_stalled(p, now):
            stalled += 1
            idle = idle_days(p.updated_at, now)
            reasons.append({"kind": "stalled", "label": f"no update in {idle}d"})
            severity = max(severity, 1)
            days = max(days, idle)
        overdue_ms = overdue_milestones(p, today)
        if overdue_ms:
            off_track += 1
            # Surface the most overdue milestone by name — more actionable than a
            # bare count.
            worst = min(overdue_ms, key=lambda m: m.target_date)  # type: ignore[arg-type]
            late = (today - worst.target_date).days  # type: ignore[operator]
            reasons.append(
                {"kind": "off_track", "label": f"{worst.name} {late}d overdue"}
            )
            severity = max(severity, 3)
            days = max(days, late)
        if reasons:
            attention.append(
                {
                    "project_id": p.id,
                    "customer": p.customer.name,
                    "name": p.name,
                    "status": status_name.get(p.status_id, ""),
                    "pct": prog["pct"],
                    "reasons": reasons,
                    "_severity": severity,
                    "_days": days,
                }
            )

    attention.sort(key=lambda a: (a["_severity"], a["_days"]), reverse=True)

    # Expiring external viewer accounts — admin-only, and only meaningful once
    # external invitations exist. Cheap to compute; template gates the display.
    expiring: list[dict[str, Any]] = []
    if user.is_admin:
        rows = (
            db.query(AppUser)
            .filter(
                AppUser.is_external.is_(True),
                AppUser.is_active.is_(True),
                AppUser.expires_at.isnot(None),
            )
            .all()
        )
        for u in rows:
            left = (u.expires_at.date() - today).days
            if left <= EXPIRY_SOON_DAYS:
                expiring.append(
                    {
                        "name": u.display_label,
                        "email": u.email,
                        "days_left": left,
                    }
                )
        expiring.sort(key=lambda e: e["days_left"])

    # Chart-ready series: project counts by status keep the canonical order;
    # the rest are ranked by size.
    status_series = [
        {"label": status_name[s.id], "count": status_counter[s.id]}
        for s in statuses
        if status_counter.get(s.id)
    ]
    # Projects by type: named types alphabetically (their canonical order — types
    # carry no sort_order), with any untyped projects bucketed last.
    type_series = [
        {"label": name, "count": type_counter[name]}
        for name in sorted(n for n in type_counter if n != "Untyped")
    ]
    if type_counter.get("Untyped"):
        type_series.append({"label": "Untyped", "count": type_counter["Untyped"]})
    se_series = [
        {"label": name, "count": n} for name, n in se_counter.most_common(SE_LOAD_TOP)
    ]
    uc_status_series = [
        {"label": name, "count": n} for name, n in uc_status_counter.most_common()
    ]
    feature_series = [
        {"label": name, "count": n} for name, n in feature_counter.most_common()
    ]

    return {
        "kpis": {
            "active": len(projects),
            "avg_completion": round(sum(pcts) / len(pcts)) if pcts else 0,
            "at_risk": at_risk,
            "stalled": stalled,
            "off_track": off_track,
        },
        "status_series": status_series,
        "type_series": type_series,
        "se_series": se_series,
        "uc_status_series": uc_status_series,
        "feature_series": feature_series,
        "attention": attention[:ATTENTION_LIMIT],
        "attention_total": len(attention),
        "expiring": expiring,
        "stalled_days": STALLED_DAYS,
        "has_data": bool(projects),
    }


@router.get("")
@router.get("/")
def dashboard(
    request: Request,
    scope: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()

    selected_status_ids = prefs.get("status_ids")
    visible_statuses = [
        s for s in statuses
        if selected_status_ids is None or s.id in selected_status_ids
    ]
    visible_statuses = _order_statuses(visible_statuses, prefs.get("status_order"))

    # Project scope: mine (default) / all / unassigned / a specific engineer.
    # External viewers ignore scope and only ever see projects shared with them.
    scope = resolve_scope(db, user, scope)
    visible_ids = scoped_project_ids(db, user, scope)

    sort = prefs.get("sort", DEFAULT_SORT)
    groups = []
    for status in visible_statuses:
        q = (
            db.query(Project)
            .filter(Project.status_id == status.id, Project.is_archived.is_(False))
        )
        if visible_ids is not None:
            q = q.filter(Project.id.in_(visible_ids))
        if sort == "start_date":
            q = q.order_by(Project.start_date.is_(None), Project.start_date)
        elif sort == "name":
            q = q.order_by(Project.name)
        else:
            q = q.order_by(Project.updated_at.desc())
        projects = q.all()
        groups.append(
            {
                "status": status,
                "projects": [
                    {"project": p, "progress": _progress(p)} for p in projects
                ],
            }
        )

    total_q = db.query(Project).filter(Project.is_archived.is_(False))
    if visible_ids is not None:
        total_q = total_q.filter(Project.id.in_(visible_ids))
    total_active = total_q.count()

    insights = _build_insights(db, user, visible_ids, statuses)
    return render(
        request,
        "dashboard/index.html",
        current_user=user,
        active_section="dashboard",
        groups=groups,
        prefs=prefs,
        all_columns=ALL_COLUMNS,
        total_active=total_active,
        scope=scope,
        scope_engineers=selectable_engineers(db, user),
        insights=insights,
    )


@router.get("/preferences")
def preferences_form(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    prefs = _load_prefs(db, user)
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    statuses = _order_statuses(statuses, prefs.get("status_order"))
    return render(
        request,
        "dashboard/preferences.html",
        current_user=user,
        active_section="dashboard",
        prefs=prefs,
        statuses=statuses,
        all_columns=ALL_COLUMNS,
    )


@router.post("/preferences")
async def save_preferences(
    request: Request,
    sort: str = Form(DEFAULT_SORT),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    form = await request.form()
    columns = [c["key"] for c in ALL_COLUMNS if form.get(f"col_{c['key']}")]
    status_values = form.getlist("status_ids")  # type: ignore[attr-defined]
    status_ids = [int(s) for s in status_values] if status_values else None

    order_raw = form.get("status_order", "")
    status_order = [int(x) for x in str(order_raw).split(",") if x.strip().isdigit()] or None

    config = {
        "columns": columns or DEFAULT_COLUMNS,
        "status_ids": status_ids,
        "status_order": status_order,
        "sort": sort if sort in {"updated", "start_date", "name"} else DEFAULT_SORT,
        # Preserve the user's My/All POC scope, which lives in the same blob but
        # is toggled from the dashboard rather than this preferences form.
        "scope": get_scope(db, user),
    }
    row = (
        db.query(DashboardPref)
        .filter(DashboardPref.app_user_id == user.id)
        .one_or_none()
    )
    if row is None:
        row = DashboardPref(app_user_id=user.id)
        db.add(row)
    row.config_json = json.dumps(config)
    db.commit()
    flash(request, "Dashboard preferences saved.", "success")
    return RedirectResponse(url="/ui/dashboard", status_code=303)
