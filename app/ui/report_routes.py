"""HTML UI for reporting: an all-POCs overview and a single-POC detail report.

Both reports are print-friendly (use the browser's Print to PDF). The single
report shows everything captured for one POC in a clean layout.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Project, ProjectStatus
from app.ui.dependencies import require_ui_user
from app.ui.project_routes import _get_project, _grouped_use_cases
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/reports", tags=["ui"], include_in_schema=False)


def _progress(project: Project) -> dict[str, int]:
    total = len(project.use_cases)
    done = sum(1 for uc in project.use_cases if uc.status and uc.status.is_complete_status)
    return {"total": total, "done": done, "pct": round(done / total * 100) if total else 0}


@router.get("/")
def all_pocs(
    request: Request,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    query = db.query(Project)
    if not include_archived:
        query = query.filter(Project.is_archived.is_(False))
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    order = {s.id: s.sort_order for s in statuses}
    projects = sorted(
        query.all(),
        key=lambda p: (order.get(p.status_id, 999), p.customer.name.lower()),
    )
    rows = [{"project": p, "progress": _progress(p)} for p in projects]
    return render(
        request, "reports/all.html", current_user=user, active_section="reports",
        rows=rows, include_archived=include_archived,
        generated_for=user.username,
    )


@router.get("/projects/{project_id}")
def project_report(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    project = _get_project(db, project_id)
    return render(
        request, "reports/project.html", current_user=user, active_section="reports",
        project=project, use_case_groups=_grouped_use_cases(project),
        progress=_progress(project), generated_for=user.username,
    )
