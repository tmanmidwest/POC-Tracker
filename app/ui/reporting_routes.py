"""UI for the report builder: list, build, run, export, share, and schedule.

Mounted under ``/ui/reports`` alongside the older per-project report routes
(``report_routes.py``). Those own ``/``, ``/analytics``, and ``/projects/*``; this
router adds the saved-report surfaces under distinct paths (``/saved``, ``/new``,
``/{id}/...``), so the two never collide.

Every route enforces **two** gates: the capability (``report.view`` /
``report.create`` / ``report.publish`` / ``report.schedule`` via
``require_capability``) and, for a specific report, the per-report visibility /
ownership check in ``reporting.saved``. The engine re-applies region scope on
every run, so nothing here can widen what a viewer sees.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.models import AppUser, Region, SavedReport
from app.models.saved_report import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLISHED,
    VISIBILITY_SHARED,
)
from app.models.app_user import ROLE_MANAGER, ROLE_STANDARD
from app.db import get_db
from app.services import email as email_service
from app.services.audit import record_event
from app.services.reporting import engine, exports
from app.services.reporting import registry as reg
from app.services.reporting import saved
from app.ui.dependencies import require_capability, require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/reports", tags=["ui"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fields_payload() -> dict:
    """Registry metadata the builder JS needs, per entity."""
    out: dict = {}
    for entity in reg.ENTITIES:
        out[entity] = [
            {
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "source": f.source,
                "ops": list(f.ops),
                "filterable": f.filterable,
                "groupable": f.groupable,
                "sortable": f.sortable,
                "measurable": f.is_measurable,
                "help": f.help,
            }
            for f in reg.fields_for(entity)
        ]
    return out


def _builder_context(user: AppUser, db: Session) -> dict:
    """Shared context for the builder page (new + edit)."""
    regions = db.query(Region).order_by(Region.name).all()
    return {
        "current_user": user,
        "active_section": "reports",
        "fields_json": json.dumps(_fields_payload()),
        "op_labels_json": json.dumps(reg.OP_LABELS),
        "measure_labels_json": json.dumps(reg.MEASURE_LABELS),
        "entity_labels_json": json.dumps(reg.ENTITY_LABELS),
        "entities": reg.ENTITIES,
        "entity_labels": reg.ENTITY_LABELS,
        "regions": regions,
        "share_roles": [(ROLE_MANAGER, "Managers"), (ROLE_STANDARD, "Sales engineers")],
        "can_publish": user.can("report.publish"),
        "can_schedule": user.can("report.schedule"),
    }


def _definition_from_form(form) -> tuple[str, bool, dict]:
    entity = form.get("entity") or ""
    is_summary = str(form.get("is_summary")) in ("1", "true", "on")
    try:
        definition = json.loads(form.get("definition_json") or "{}")
    except (ValueError, TypeError):
        definition = {}
    if not isinstance(definition, dict):
        definition = {}
    return entity, is_summary, definition


def _audience_from_form(form) -> dict:
    return {
        "role_keys": form.getlist("audience_roles"),
        "region_ids": form.getlist("audience_regions"),
        "kind": form.get("audience_kind"),
    }


def _load_viewable(db: Session, user: AppUser, report_id: int) -> SavedReport:
    report = saved.get_report(db, report_id)
    if report is None or not saved.can_view_report(db, user, report):
        raise HTTPException(status_code=404, detail="Report not found.")
    return report


def _load_editable(db: Session, user: AppUser, report_id: int) -> SavedReport:
    report = saved.get_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found.")
    if not saved.can_edit_report(user, report):
        raise HTTPException(status_code=403, detail="Not your report.")
    return report


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/saved")
def saved_list(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.view")),
) -> Response:
    reports = saved.visible_reports(db, user)
    return render(
        request, "reports/saved_list.html", current_user=user,
        active_section="reports", reports=reports,
        can_create=user.can("report.create"),
        entity_labels=reg.ENTITY_LABELS,
    )


# ---------------------------------------------------------------------------
# Builder (new / edit)
# ---------------------------------------------------------------------------


@router.get("/new")
def new_report(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.create")),
) -> Response:
    ctx = _builder_context(user, db)
    return render(
        request, "reports/builder.html", report=None, report_json="null", **ctx
    )


@router.get("/{report_id}/edit")
def edit_report(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.create")),
) -> Response:
    report = _load_editable(db, user, report_id)
    ctx = _builder_context(user, db)
    report_json = json.dumps(
        {
            "id": report.id,
            "name": report.name,
            "description": report.description or "",
            "entity": report.entity,
            "is_summary": report.is_summary,
            "definition": report.definition or {},
            "visibility": report.visibility,
            "audience": report.audience or {},
        }
    )
    return render(request, "reports/builder.html", report=report, report_json=report_json, **ctx)


# ---------------------------------------------------------------------------
# Live preview (htmx partial)
# ---------------------------------------------------------------------------


@router.post("/preview")
async def preview(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.view")),
) -> Response:
    form = await request.form()
    entity, is_summary, definition = _definition_from_form(form)
    if not reg.is_valid_entity(entity):
        return render(request, "reports/_preview.html", current_user=user, error="Pick something to report on.", result=None)
    try:
        clean = reg.validate_definition(entity, definition, summary=is_summary)
    except reg.DefinitionError as exc:
        return render(request, "reports/_preview.html", current_user=user, error=str(exc), result=None)
    result = engine.run(db, user, entity, clean, is_summary=is_summary, limit=engine.PREVIEW_LIMIT)
    return render(
        request, "reports/_preview.html", current_user=user, result=result,
        error=None, preview=True, preview_limit=engine.PREVIEW_LIMIT,
    )


@router.get("/options")
def field_options(
    request: Request,
    entity: str,
    field: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.view")),
) -> Response:
    """Datalist <option>s for a filter value (region-scoped distinct values)."""
    opts = engine.enum_options(db, user, entity, field) if reg.is_valid_entity(entity) else []
    body = "".join(f'<option value="{o}"></option>' for o in opts)
    return Response(content=body, media_type="text/html")


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------


def _resolve_visibility(user: AppUser, requested: str) -> str:
    """Downgrade shared/published to private if the user can't publish."""
    if requested in (VISIBILITY_SHARED, VISIBILITY_PUBLISHED) and not user.can("report.publish"):
        return VISIBILITY_PRIVATE
    if requested in (VISIBILITY_PRIVATE, VISIBILITY_SHARED, VISIBILITY_PUBLISHED):
        return requested
    return VISIBILITY_PRIVATE


@router.post("")
@router.post("/")
async def create(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.create")),
) -> Response:
    form = await request.form()
    entity, is_summary, definition = _definition_from_form(form)
    visibility = _resolve_visibility(user, form.get("visibility") or VISIBILITY_PRIVATE)
    try:
        report = saved.create_report(
            db, user,
            name=form.get("name") or "",
            description=form.get("description"),
            entity=entity,
            is_summary=is_summary,
            definition=definition,
            visibility=visibility,
            audience=_audience_from_form(form),
        )
    except ValueError as exc:
        flash(request, f"Couldn't save the report: {exc}", "error")
        return RedirectResponse(url="/ui/reports/new", status_code=303)
    _audit(user, report, "report.created")
    flash(request, f"Saved “{report.name}”.", "success")
    return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)


@router.post("/{report_id}")
async def update(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.create")),
) -> Response:
    report = _load_editable(db, user, report_id)
    form = await request.form()
    _, is_summary, definition = _definition_from_form(form)
    visibility = _resolve_visibility(user, form.get("visibility") or report.visibility)
    try:
        saved.update_report(
            db, report,
            name=form.get("name") or "",
            description=form.get("description"),
            definition=definition,
            is_summary=is_summary,
            visibility=visibility,
            audience=_audience_from_form(form),
        )
    except ValueError as exc:
        flash(request, f"Couldn't save the report: {exc}", "error")
        return RedirectResponse(url=f"/ui/reports/{report.id}/edit", status_code=303)
    _audit(user, report, "report.updated")
    flash(request, "Report updated.", "success")
    return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)


@router.post("/{report_id}/delete")
async def delete(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.create")),
) -> Response:
    report = _load_editable(db, user, report_id)
    name = report.name
    saved.delete_report(db, report)
    _audit(user, report, "report.deleted", target_label=name)
    flash(request, f"Deleted “{name}”.", "success")
    return RedirectResponse(url="/ui/reports/saved", status_code=303)


# ---------------------------------------------------------------------------
# Run + export
# ---------------------------------------------------------------------------


@router.get("/{report_id}/run")
def run_report(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.view")),
) -> Response:
    report = _load_viewable(db, user, report_id)
    result = engine.run(db, user, report.entity, report.definition, is_summary=report.is_summary)
    return render(
        request, "reports/run.html", current_user=user, active_section="reports",
        report=report, result=result, entity_labels=reg.ENTITY_LABELS,
        can_edit=saved.can_edit_report(user, report),
        can_schedule=user.can("report.schedule"),
    )


@router.get("/{report_id}/export.{fmt}")
def export_report(
    report_id: int,
    fmt: str,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.view")),
) -> Response:
    report = _load_viewable(db, user, report_id)
    result = engine.run(db, user, report.entity, report.definition, is_summary=report.is_summary)
    stem = _safe_filename(report.name)
    tag = date.today().strftime("%m%d%Y")
    if fmt == "csv":
        data, media, ext = exports.to_csv(result), "text/csv", "csv"
    elif fmt == "xlsx":
        data = exports.to_xlsx(result, title=report.name)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    elif fmt == "pdf":
        try:
            data = exports.to_pdf(result, title=report.name, subtitle=_subtitle(report, result))
        except Exception:  # pragma: no cover - depends on system libs
            log.exception("report_export_pdf_failed", extra={"report_id": report_id})
            raise HTTPException(status_code=500, detail="PDF generation failed.") from None
        media, ext = "application/pdf", "pdf"
    else:
        raise HTTPException(status_code=404, detail="Unknown format.")
    return Response(
        content=data, media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{stem}-{tag}.{ext}"',
            "Cache-Control": "no-store, must-revalidate",
        },
    )


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@router.post("/{report_id}/schedule")
async def schedule(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.schedule")),
) -> Response:
    report = _load_editable(db, user, report_id)
    form = await request.form()
    recipients = [
        e.strip() for e in (form.get("recipients") or "").replace(";", ",").split(",") if e.strip()
    ]
    if not recipients:
        flash(request, "Add at least one recipient email.", "error")
        return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)
    if not email_service.is_ready(db):
        flash(request, "Email isn't configured yet — set up SMTP in Settings first.", "error")
        return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)
    fmt = form.get("format") if form.get("format") in ("xlsx", "csv", "pdf") else "xlsx"
    freq = form.get("freq") if form.get("freq") in ("daily", "weekly", "monthly") else "weekly"
    schedule_dict = {
        "active": True,
        "freq": freq,
        "day": int(form.get("day") or 0),      # weekly: 0=Mon..6=Sun; monthly: day-of-month
        "hour": max(0, min(23, int(form.get("hour") or 8))),
        "recipients": recipients,
        "format": fmt,
        "last_sent_on": None,
    }
    saved.set_schedule(db, report, schedule_dict)
    _audit(user, report, "report.scheduled", detail={"freq": freq, "recipients": len(recipients)})
    flash(request, f"Scheduled — {len(recipients)} recipient(s), {freq}.", "success")
    return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)


@router.post("/{report_id}/unschedule")
async def unschedule(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_capability("report.schedule")),
) -> Response:
    report = _load_editable(db, user, report_id)
    saved.set_schedule(db, report, None)
    _audit(user, report, "report.unscheduled")
    flash(request, "Schedule removed.", "success")
    return RedirectResponse(url=f"/ui/reports/{report.id}/run", status_code=303)


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------


def _safe_filename(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_ " else "" for c in (name or "report"))
    return keep.strip().replace(" ", "-").lower()[:60] or "report"


def _subtitle(report: SavedReport, result: engine.RunResult) -> str:
    n = result.total_matched
    scope = ""
    if result.scope and result.scope.region_scoped:
        scope = f" · scoped to {', '.join(result.scope.region_names) or 'no regions'}"
    return f"{n} {reg.ENTITY_LABELS.get(report.entity, report.entity).lower()}{scope}"


def _audit(user: AppUser, report: SavedReport, event_type: str, *, target_label: str | None = None, detail: dict | None = None) -> None:
    try:
        record_event(
            category="report", event_type=event_type, outcome="success",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="saved_report", target_id=report.id,
            target_label=target_label or report.name,
            message=f"{event_type} '{target_label or report.name}'",
            detail=detail or {},
        )
    except Exception:  # audit must never break the request
        log.debug("report_audit_failed", extra={"event": event_type})
