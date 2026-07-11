"""Time-based expiration for external viewer accounts.

External users get a fixed account lifetime (default 60 days, configurable via
``system_config.current_external_user_ttl_days``). A once-a-day sweep — run at
startup and every 24h from the app lifespan, alongside the audit-retention loop —
does two things:

* **Expire**: deactivate (``is_active = False``) any active external account past
  its ``expires_at``. Login is already gated on ``is_active``, so this cuts access
  immediately; it's fully reversible via :func:`extend_user`.
* **Warn**: email the Sales Engineer(s) of an expiring account's granted projects
  ``WARNING_DAYS`` before expiry, once per term (tracked by
  ``expiry_warning_sent_at``, cleared on extend). Falls back to admins when no SE
  has an email, and is skipped silently when SMTP isn't configured.

Everything here is defensive: the sweep owns its own session and never raises, so
a bad row or a dead SMTP server can't take down the app.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppUser, ProjectGrant
from app.services import email as email_service
from app.services import system_config
from app.services.audit import record_event

log = logging.getLogger(__name__)

# Days before expiry to warn the project SE.
WARNING_DAYS = 7
# Preset extension lengths offered in the UI.
EXTEND_PRESETS = (30, 60, 90)


def _now() -> datetime:
    return datetime.now(UTC)


def default_expiry_from_now() -> datetime | None:
    """The expiry to stamp on a newly-accepted external account (None = never)."""
    days = system_config.current_external_user_ttl_days()
    return _now() + timedelta(days=days) if days > 0 else None


def resolve_extension(preset: str | None, until: str | None) -> datetime:
    """Turn UI inputs into a future expiry datetime.

    ``until`` (an ISO ``YYYY-MM-DD`` date) wins when given; otherwise ``preset``
    is a number of days (one of EXTEND_PRESETS, else the configured default term,
    else 60). Raises ValueError if the result isn't in the future.
    """
    if until:
        parsed = datetime.strptime(until.strip(), "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=UTC
        )
        if parsed <= _now():
            raise ValueError("The new expiry date must be in the future.")
        return parsed
    try:
        days = int(preset) if preset else 0
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        days = system_config.current_external_user_ttl_days() or 60
    return _now() + timedelta(days=days)


def set_initial_expiry(user: AppUser) -> None:
    """Set an external user's expiry to the default term (called at acceptance)."""
    if user.is_external:
        user.expires_at = default_expiry_from_now()
        user.expiry_warning_sent_at = None


# ---------------------------------------------------------------------------
# The daily sweep
# ---------------------------------------------------------------------------


def expire_due_users(db: Session) -> int:
    """Deactivate active external accounts past their expiry. Returns the count."""
    now = _now()
    due = (
        db.query(AppUser)
        .filter(
            AppUser.is_external.is_(True),
            AppUser.is_active.is_(True),
            AppUser.expires_at.isnot(None),
            AppUser.expires_at < now,
        )
        .all()
    )
    for user in due:
        user.is_active = False
        record_event(
            category="user",
            event_type="external_user.expired",
            actor_type="system",
            actor_label="expiry-sweep",
            target_type="user",
            target_id=user.id,
            target_label=user.email or user.username,
            message=f"External user {user.email or user.username} expired and was deactivated",
        )
    if due:
        db.commit()
        log.info("external_users_expired", extra={"count": len(due)})
    return len(due)


def send_expiry_warnings(db: Session) -> int:
    """Warn SEs about accounts expiring within WARNING_DAYS. Returns emails sent."""
    if not email_service.is_ready(db):
        return 0
    now = _now()
    horizon = now + timedelta(days=WARNING_DAYS)
    soon = (
        db.query(AppUser)
        .filter(
            AppUser.is_external.is_(True),
            AppUser.is_active.is_(True),
            AppUser.expires_at.isnot(None),
            AppUser.expires_at > now,          # not already expired
            AppUser.expires_at <= horizon,     # within the warning window
            AppUser.expiry_warning_sent_at.is_(None),  # not yet warned this term
        )
        .all()
    )
    sent = 0
    for user in soon:
        recipients, projects = _warning_recipients(db, user)
        if recipients:
            try:
                _send_warning_email(db, user, recipients, projects)
                sent += 1
            except email_service.EmailError as exc:
                log.warning("expiry_warning_email_failed", extra={"user_id": user.id})
                record_event(
                    category="user",
                    event_type="external_user.expiry_warn_failed",
                    outcome="failure",
                    actor_type="system", actor_label="expiry-sweep",
                    target_type="user", target_id=user.id,
                    target_label=user.email or user.username,
                    message=(
                        f"Failed to send expiry warning for external user "
                        f"{user.email or user.username}"
                    ),
                    detail={"recipients": [r[0] for r in recipients], "error": str(exc)},
                )
                continue  # leave the flag unset so we retry next sweep
        # Stamp even when there were no recipients, so we don't recompute daily;
        # an in-app indicator still surfaces the pending expiry.
        user.expiry_warning_sent_at = now
        record_event(
            category="user",
            event_type="external_user.expiry_warned",
            actor_type="system",
            actor_label="expiry-sweep",
            target_type="user",
            target_id=user.id,
            target_label=user.email or user.username,
            message=(
                f"Warned {len(recipients)} recipient(s) that external user "
                f"{user.email or user.username} expires soon"
            ),
            detail={"recipients": [r[0] for r in recipients]},
        )
    if soon:
        db.commit()
        log.info("external_expiry_warnings", extra={"candidates": len(soon), "sent": sent})
    return sent


def run_sweep() -> None:
    """Run the full expiry sweep in its own session. Never raises."""
    from app.db import get_session_factory

    db = get_session_factory()()
    try:
        expire_due_users(db)
        send_expiry_warnings(db)
    except Exception:  # a bad row must not take down the loop
        log.exception("external_expiry_sweep_failed")
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Extend (UI-driven)
# ---------------------------------------------------------------------------


def extend_user(
    db: Session,
    user: AppUser,
    *,
    until: datetime,
    actor: AppUser,
    request=None,
) -> None:
    """Set a new expiry, clear the warning flag, and reactivate the account."""
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    was_expired = not user.is_active
    user.expires_at = until
    user.expiry_warning_sent_at = None
    user.is_active = True
    db.commit()
    record_event(
        category="user",
        event_type="external_user.extended",
        actor_type="user",
        actor_label=actor.username,
        actor_id=actor.id,
        target_type="user",
        target_id=user.id,
        target_label=user.email or user.username,
        message=(
            f"Extended external user {user.email or user.username} "
            f"until {until.date().isoformat()}"
            + (" (reactivated)" if was_expired else "")
        ),
        detail={"surface": "ui", "reactivated": was_expired},
        request=request,
    )
    log.info(
        "external_user_extended",
        extra={"user_id": user.id, "until": until.isoformat(), "by": actor.username},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _warning_recipients(
    db: Session, user: AppUser
) -> tuple[list[tuple[str, str]], list[str]]:
    """(recipients, project_names) for a user's expiry warning.

    Recipients are the distinct Sales Engineers (with an email) across every
    project the user can view; if none have an email, fall back to admins.
    """
    grants = db.query(ProjectGrant).filter(ProjectGrant.user_id == user.id).all()
    projects = [g.project for g in grants if g.project]
    project_names = [p.display_name for p in projects]

    recipients: dict[str, str] = {}
    for project in projects:
        se = project.sales_engineer
        if se and se.email:
            recipients.setdefault(se.email, se.display_label)
    if not recipients:
        admins = (
            db.query(AppUser)
            .filter(
                AppUser.is_admin.is_(True),
                AppUser.is_active.is_(True),
                AppUser.email.isnot(None),
            )
            .all()
        )
        for admin in admins:
            recipients.setdefault(admin.email, admin.display_label)
    return list(recipients.items()), project_names


def _send_warning_email(
    db: Session, user: AppUser, recipients: list[tuple[str, str]], projects: list[str]
) -> None:
    days = user.days_until_expiry
    when = user.expires_at_aware.date().isoformat() if user.expires_at_aware else "soon"
    who = user.display_label
    app_name = get_settings().app_name
    proj_line = ", ".join(projects) if projects else "their shared project(s)"

    subject = f"{app_name}: external user {who} expires in {days} day(s)"
    text = (
        f"Heads up — the external viewer account for {who} ({user.email or user.username}) "
        f"is scheduled to expire on {when} ({days} day(s) from now).\n\n"
        f"Projects: {proj_line}\n\n"
        "When it expires they'll lose access until the account is extended. You can "
        "extend it from the project's Shared access panel in Questlog, or an admin "
        "can extend it under Settings → Users.\n"
    )
    html = (
        f"<p>Heads up — the external viewer account for <strong>{who}</strong> "
        f"({user.email or user.username}) is scheduled to expire on <strong>{when}</strong> "
        f"({days} day(s) from now).</p>"
        f"<p><strong>Projects:</strong> {proj_line}</p>"
        "<p>When it expires they'll lose access until the account is extended. You can "
        "extend it from the project's <em>Shared access</em> panel in Questlog, or an "
        "admin can extend it under <em>Settings → Users</em>.</p>"
    )
    for to_email, _name in recipients:
        email_service.send_email(
            db, to=to_email, subject=subject, text_body=text, html_body=html
        )
