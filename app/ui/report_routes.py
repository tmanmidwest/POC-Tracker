"""HTML UI for reporting: an all-POCs overview and a single-POC detail report.

Both reports are print-friendly (use the browser's Print to PDF). The single
report shows everything captured for one POC in a clean layout.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Project, ProjectStatus
from app.services import (
    report_archive,
    report_docx,
    report_pdf,
    report_pptx,
    report_template,
)
from app.services.ai import readout as ai_readout
from app.services.ai.summaries import default_provider
from app.services import use_case_io
from app.services.audit import record_event
from app.services.access import accessible_project_ids, notes_for_report
from app.services.branding import current_branding
from app.services.tasks import tasks_for_report
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


def _resolve_include_internal(user: AppUser, audience: str | None) -> bool:
    """Whether a report should include internal-only items.

    The audience is an explicit choice ("client" vs "internal"), decoupled from
    who is generating the report — but it can only ever *reduce* what an external
    viewer sees, never elevate it. Internal content is included only when an
    internal user explicitly asks for the internal audience; the default (and any
    request from an external viewer) is the client-facing, internal-excluded
    report.
    """
    return bool(user.is_internal) and str(audience).lower() == "internal"


def _report_context(
    db: Session, project: Project, user: AppUser, *, include_internal: bool
) -> dict[str, Any]:
    """Context shared by the on-screen report, the PDF, and the zip.

    ``include_internal`` carries the report's audience: internal-only notes and
    tasks are included only for an internal audience, and never for an external
    viewer (see :func:`_resolve_include_internal`).
    """
    notes = notes_for_report(project, user, include_internal=include_internal)
    tasks = tasks_for_report(db, project, user, include_internal=include_internal)
    from app.services import customer_logo

    return {
        "project": project,
        "notes": notes,
        "tasks": tasks,
        "include_internal": include_internal,
        "use_case_groups": _grouped_use_cases(project),
        "progress": _progress(project),
        "generated_for": user.username,
        "generated_on": date.today().strftime("%b %-d, %Y"),
        "has_artifacts": report_archive.project_has_artifacts(project, notes),
        "branding": current_branding(),
        "customer_logo": customer_logo.data_uri(project.customer_id),
        # Per-render token appended to the PDF/zip download links so the browser
        # can never serve a cached copy from a previous (pre-fix) download.
        "cache_bust": str(int(time.time())),
    }


def _download_headers(
    filename: str, *, suffix: str, internal: bool = False
) -> dict[str, str]:
    base = report_archive._safe(filename, fallback="project")
    # Internal-audience exports carry an "-internal" marker so a copy that includes
    # internal-only content is unmistakable and not mistaken for a client-safe one.
    stem, _, ext = suffix.rpartition(".")
    if internal:
        stem = f"{stem or suffix}-internal"
    # Stamp the export date (MMDDYYYY) + a random 4-digit token before the extension
    # (e.g. ...-report-internal-06292026-4823.pdf) so every export filename is unique.
    tag = f"{date.today().strftime('%m%d%Y')}-{random.randint(1000, 9999)}"
    name = f"{base}-{stem}-{tag}.{ext}" if ext else f"{base}-{stem}-{tag}"
    return {
        "Content-Disposition": f'attachment; filename="{name}"',
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
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Standalone, nav-free report page (opened in a new tab).

    ``audience`` selects a client-facing report (``client``, the default —
    internal-only items excluded) or an internal one (``internal`` — everything
    included). Only internal users can produce the internal audience.
    """
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    return render(
        request, "reports/project.html", current_user=user,
        ai_available=default_provider(db) is not None,
        # The audience chooser is only meaningful for internal users; external
        # viewers always get the client-facing report and never see the toggle.
        can_choose_audience=user.is_internal,
        audience="internal" if include_internal else "client",
        **_report_context(db, project, user, include_internal=include_internal),
    )


@router.get("/projects/{project_id}/pdf")
def project_report_pdf(
    project_id: int,
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Server-rendered PDF of the full project report."""
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    html = report_pdf.render_report_html(
        _report_context(db, project, user, include_internal=include_internal)
    )
    try:
        pdf = report_pdf.project_report_pdf(project, html)
    except Exception:  # pragma: no cover - depends on system libs
        log.exception("report_pdf_failed", extra={"project_id": project_id})
        raise HTTPException(status_code=500, detail="PDF generation failed.") from None
    return Response(
        content=pdf, media_type="application/pdf",
        headers=_download_headers(
            project.display_name, suffix="report.pdf", internal=include_internal
        ),
    )


@router.get("/projects/{project_id}/report.docx")
def project_report_docx(
    project_id: int,
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Editable Word (.docx) version of the full project report, with screenshots."""
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    data = report_docx.build_project_report_docx(
        project, _report_context(db, project, user, include_internal=include_internal)
    )
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=_download_headers(
            project.display_name, suffix="report.docx", internal=include_internal
        ),
    )


@router.get("/projects/{project_id}/tracker.xlsx")
def project_tracker_xlsx(
    project_id: int,
    mode: str = "working",
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """A polished, two-tab Excel use-case tracker (Summary + Use case tracker).

    ``mode=working`` (the default) is an editable copy with live status
    dropdowns; ``mode=readonly`` is a protected snapshot for sharing. Comments
    are always included; the audience only marks the filename as internal.
    """
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    editable = str(mode).lower() != "readonly"
    data = use_case_io.build_project_tracker_xlsx(
        db, project, _grouped_use_cases(project), editable=editable
    )
    suffix = "use-case-tracker.xlsx" if editable else "use-case-tracker-readonly.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_download_headers(
            project.display_name, suffix=suffix, internal=include_internal
        ),
    )


@router.get("/projects/{project_id}/readout.pptx")
def project_readout_pptx(
    project_id: int,
    ai: bool = False,
    theme: str = "light",
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Executive readout deck (.pptx): scorecard, results by category, and proof.

    With ``?ai=1`` and a configured AI provider, the summary/next-steps slides are
    enriched with generated bullets; on any failure it falls back silently to the
    deterministic deck, so a download is always produced. ``?theme=dark`` renders a
    dark-palette deck (uses the template's dark layout if it has one).
    """
    theme = "dark" if str(theme).lower() == "dark" else "light"
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    narrative = None
    if ai:
        try:
            narrative = ai_readout.generate_readout_narrative(db, project)
        except Exception as exc:  # never let AI trouble block a download
            log.exception("readout_narrative_failed", extra={"project_id": project_id})
            # Warning, not failure: the deck still downloads without AI bullets.
            record_event(
                category="project", event_type="readout.narrative_failed", outcome="warning",
                actor_type="user", actor_label=user.username, actor_id=user.id,
                target_type="project", target_id=project.id, target_label=project.display_name,
                message=f"Readout AI narrative failed for '{project.display_name}'; "
                        "produced the deck without it",
                detail={"surface": "ui", "error": str(exc)},
            )
    from app.services import customer_logo

    cust_logo_path = (
        str(customer_logo.path_for(project.customer_id))
        if customer_logo.has_logo(project.customer_id)
        else None
    )
    data = report_pptx.build_project_readout_pptx(
        project,
        _report_context(db, project, user, include_internal=include_internal),
        narrative=narrative,
        template_path=report_template.template_path_if_present(),
        logo_path=report_template.logo_path_if_present(),
        customer_logo_path=cust_logo_path,
        theme=theme,
    )
    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        headers=_download_headers(
            project.display_name, suffix="readout.pptx", internal=include_internal
        ),
    )


@router.get("/projects/{project_id}/artifacts.zip")
def project_report_archive(
    project_id: int,
    audience: str = "client",
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """A single zip with the report PDF, all screenshots, and all attachments.

    Follows the report's audience: a client-facing zip excludes the PDF's
    internal-only notes/tasks and the attachments on internal-only notes.
    """
    project = _get_viewable_project(db, project_id, user)
    include_internal = _resolve_include_internal(user, audience)
    ctx = _report_context(db, project, user, include_internal=include_internal)
    html = report_pdf.render_report_html(ctx)
    pdf: bytes | None = None
    try:
        pdf = report_pdf.project_report_pdf(project, html)
    except Exception:  # pragma: no cover - still bundle files even if PDF fails
        log.exception("report_pdf_failed_in_zip", extra={"project_id": project_id})
    data = report_archive.build_project_archive(project, pdf, ctx["notes"])
    return Response(
        content=data, media_type="application/zip",
        headers=_download_headers(
            project.display_name, suffix="artifacts.zip", internal=include_internal
        ),
    )
