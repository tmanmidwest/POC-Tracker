"""Scheduled report delivery — the background sweep that emails due reports.

Driven by a loop in ``app.main`` (the same pattern as the audit/expiry/Google
sweeps) and **guarded by a Postgres advisory lock** so exactly one app instance
sends, no matter how many are running. The sweep:

- finds active-scheduled reports that are *due* (by freq/day/hour, not already
  sent today),
- renders each **per resolved recipient scope** — an internal recipient sees only
  rows their region access allows; an address that isn't an app user gets the
  owner's view (the owner authored the distribution),
- emails the chosen format as an attachment via the SMTP service,
- stamps ``last_sent_on`` and records an audit event per send.

Resilience mirrors the other sweeps: it owns its session, never raises, and one
failing report can't stop the others or kill the loop.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models import AppUser, SavedReport
from app.services import email as email_service
from app.services.audit import record_event
from app.services.reporting import engine, exports

log = logging.getLogger(__name__)

_FORMAT_MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "pdf": "application/pdf",
}


def _is_due(schedule: dict, now: datetime) -> bool:
    """Whether a schedule should fire at ``now`` and hasn't already today."""
    if not schedule or not schedule.get("active"):
        return False
    if now.hour < int(schedule.get("hour", 8)):
        return False
    if schedule.get("last_sent_on") == now.date().isoformat():
        return False
    freq = schedule.get("freq", "weekly")
    if freq == "daily":
        return True
    if freq == "weekly":
        return now.weekday() == int(schedule.get("day", 0))
    if freq == "monthly":
        return now.day == max(1, min(28, int(schedule.get("day", 1)) or 1))
    return False


def _render(result: engine.RunResult, report: SavedReport, fmt: str) -> bytes:
    if fmt == "csv":
        return exports.to_csv(result)
    if fmt == "pdf":
        return exports.to_pdf(result, title=report.name)
    return exports.to_xlsx(result, title=report.name)


def _deliver_one(db: Session, report: SavedReport, now: datetime) -> None:
    """Render + email a single due report to its recipients (never raises)."""
    schedule = report.schedule or {}
    recipients = [r for r in (schedule.get("recipients") or []) if r]
    if not recipients or report.owner is None:
        return
    fmt = schedule.get("format") if schedule.get("format") in _FORMAT_MIME else "xlsx"

    # Group recipients by the user whose region scope their copy is rendered with:
    # a known internal user -> themselves; anyone else -> the report owner.
    by_scope: dict[int, tuple[AppUser, list[str]]] = {}
    for addr in recipients:
        match = (
            db.query(AppUser)
            .filter(AppUser.email == addr, AppUser.is_active.is_(True), AppUser.is_external.is_(False))
            .first()
        )
        scope_user = match or report.owner
        entry = by_scope.setdefault(scope_user.id, (scope_user, []))
        entry[1].append(addr)

    sent = 0
    for scope_user, addrs in by_scope.values():
        try:
            result = engine.run(
                db, scope_user, report.entity, report.definition,
                is_summary=report.is_summary,
            )
            data = _render(result, report, fmt)
            filename = f"{report.name[:60].strip() or 'report'}-{now.date().isoformat()}.{fmt}"
            html = exports.result_to_html(result, title=report.name,
                                          subtitle=f"{result.total_matched} rows · scheduled {schedule.get('freq')}")
            text = f"Your scheduled report “{report.name}” is attached ({result.total_matched} rows)."
        except Exception:
            log.exception("scheduled_report_render_failed", extra={"report_id": report.id})
            continue
        for addr in addrs:
            try:
                email_service.send_email(
                    db, to=addr, subject=f"{report.name} — scheduled report",
                    text_body=text, html_body=html,
                    attachments=[(filename, _FORMAT_MIME[fmt], data)],
                )
                sent += 1
                _audit(report, addr, "success")
            except Exception as exc:
                log.warning("scheduled_report_send_failed",
                            extra={"report_id": report.id, "to": addr, "error": str(exc)})
                _audit(report, addr, "failure", str(exc))

    # Stamp last_sent_on so it won't re-fire today (reassign — JSONB in-place
    # mutation isn't tracked by the ORM).
    report.schedule = {**schedule, "last_sent_on": now.date().isoformat()}
    db.commit()
    log.info("scheduled_report_sent", extra={"report_id": report.id, "recipients": sent})


def run_due_sweep(now: datetime | None = None) -> None:
    """Find and deliver every due scheduled report. Owns its session; never raises."""
    now = now or datetime.now()
    try:
        session_factory = get_session_factory()
    except Exception:  # pragma: no cover - engine not ready
        log.exception("scheduler_no_session")
        return
    with session_factory() as db:
        try:
            reports = (
                db.query(SavedReport)
                .filter(SavedReport.schedule.isnot(None))
                .all()
            )
        except Exception:  # pragma: no cover
            log.exception("scheduler_query_failed")
            return
        for report in reports:
            try:
                if _is_due(report.schedule or {}, now):
                    _deliver_one(db, report, now)
            except Exception:  # one bad report must not stop the rest
                log.exception("scheduled_report_failed", extra={"report_id": report.id})
                db.rollback()


def _audit(report: SavedReport, addr: str, outcome: str, error: str = "") -> None:
    try:
        record_event(
            category="report", event_type="report.emailed", outcome=outcome,
            actor_type="system", actor_label="report-scheduler",
            target_type="saved_report", target_id=report.id, target_label=report.name,
            message=f"Scheduled report '{report.name}' emailed to {addr}",
            detail={"to": addr, "error": error} if error else {"to": addr},
        )
    except Exception:
        log.debug("scheduler_audit_failed")
