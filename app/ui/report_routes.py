"""HTML UI for reporting: an all-POCs overview and a single-POC detail report.

Both reports are print-friendly (use the browser's Print to PDF). The single
report shows everything captured for one POC in a clean layout.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Project, ProjectStatus
from app.services import report_archive, report_pdf
from app.services.access import accessible_project_ids
from app.services.branding import current_branding
from app.ui.dependencies import require_ui_user
from app.ui.project_routes import (
    _get_viewable_project,
    _grouped_use_cases,
)
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/reports", tags=["ui"], include_in_schema=False)


def _progress(project: Project) -> dict[str, int]:
    total = len(project.use_cases)
    done = sum(1 for uc in project.use_cases if uc.status and uc.status.is_complete_status)
    return {"total": total, "done": done, "pct": round(done / total * 100) if total else 0}


def _report_context(project: Project, user: AppUser) -> dict[str, Any]:
    """Context shared by the on-screen report, the PDF, and the zip."""
    return {
        "project": project,
        "use_case_groups": _grouped_use_cases(project),
        "progress": _progress(project),
        "generated_for": user.username,
        "generated_on": date.today().strftime("%b %-d, %Y"),
        "has_artifacts": report_archive.project_has_artifacts(project),
        "branding": current_branding(),
        # Per-render token appended to the PDF/zip download links so the browser
        # can never serve a cached copy from a previous (pre-fix) download.
        "cache_bust": str(int(time.time())),
    }


def _download_headers(filename: str, *, suffix: str) -> dict[str, str]:
    base = report_archive._safe(filename, fallback="project")
    return {
        "Content-Disposition": f'attachment; filename="{base}-{suffix}"',
        # These are freshly generated each request — never let a browser serve a
        # stale cached copy (which can show an out-of-date layout).
        "Cache-Control": "no-store, must-revalidate",
        "Pragma": "no-cache",
    }


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
    # External viewers only see projects shared with them; internal users see all.
    visible_ids = accessible_project_ids(db, user)
    if visible_ids is not None:
        query = query.filter(Project.id.in_(visible_ids))
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
    """Standalone, nav-free report page (opened in a new tab)."""
    project = _get_viewable_project(db, project_id, user)
    return render(
        request, "reports/project.html", current_user=user,
        **_report_context(project, user),
    )


@router.get("/projects/{project_id}/pdf")
def project_report_pdf(
    project_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Server-rendered PDF of the full project report."""
    project = _get_viewable_project(db, project_id, user)
    html = report_pdf.render_report_html(_report_context(project, user))
    try:
        pdf = report_pdf.project_report_pdf(project, html)
    except Exception:  # pragma: no cover - depends on system libs
        log.exception("report_pdf_failed", extra={"project_id": project_id})
        raise HTTPException(status_code=500, detail="PDF generation failed.") from None
    return Response(
        content=pdf, media_type="application/pdf",
        headers=_download_headers(project.display_name, suffix="report.pdf"),
    )


@router.get("/projects/{project_id}/artifacts.zip")
def project_report_archive(
    project_id: int,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """A single zip with the report PDF, all screenshots, and all attachments."""
    project = _get_viewable_project(db, project_id, user)
    html = report_pdf.render_report_html(_report_context(project, user))
    pdf: bytes | None = None
    try:
        pdf = report_pdf.project_report_pdf(project, html)
    except Exception:  # pragma: no cover - still bundle files even if PDF fails
        log.exception("report_pdf_failed_in_zip", extra={"project_id": project_id})
    data = report_archive.build_project_archive(project, pdf)
    return Response(
        content=data, media_type="application/zip",
        headers=_download_headers(project.display_name, suffix="artifacts.zip"),
    )
