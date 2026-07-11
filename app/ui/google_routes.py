"""Per-user Google Tasks connect / disconnect (OAuth consent flow).

The user starts the OAuth flow, consents at Google, and we store their encrypted
refresh token (see google_tasks_sync.connect_store). Registered internal-only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser
from app.services import google_oauth, google_tasks_sync, system_config
from app.services.audit import record_event
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/tasks/google", tags=["ui"], include_in_schema=False)

_STATE_KEY = "_gtasks_state"
_VERIFIER_KEY = "_gtasks_verifier"


def callback_uri(request: Request) -> str:
    """The redirect URI Google must be configured with (absolute)."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/ui/tasks/google/callback"


def _record_connect_failure(
    request: Request, user: AppUser, *, reason: str, detail: dict
) -> None:
    """Write a failed-connect event to the activity log with structured detail.

    The connect flow used to swallow failures into an app-log ``log.warning`` only,
    which left admins with a bare "it failed" flash and no trail. This records the
    same detail into the activity log so a failure is diagnosable after the fact.
    """
    record_event(
        category="task",
        event_type="task.google_connect_failed",
        outcome="failure",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="google_credential",
        message=f"Google Tasks connection failed: {reason}",
        detail={"surface": "ui", "reason": reason, **detail},
        request=request,
    )


def _guard(db: Session, request: Request) -> Response | None:
    """Return a redirect if the module/integration isn't available, else None."""
    if not system_config.tasks_enabled():
        flash(request, "The Task Manager is disabled.", "error")
        return RedirectResponse(url="/ui/dashboard", status_code=303)
    if not google_oauth.is_ready(db):
        flash(
            request,
            "Google Tasks sync isn't set up yet. An admin can configure it in "
            "Settings → Google Tasks.",
            "error",
        )
        return RedirectResponse(url="/ui/tasks", status_code=303)
    return None


@router.get("/connect")
def connect(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Kick off the Google consent flow."""
    blocked = _guard(db, request)
    if blocked is not None:
        return blocked

    config = google_oauth.get_config(db)
    state = google_oauth.make_state()
    verifier, challenge = google_oauth.make_pkce_pair()
    request.session[_STATE_KEY] = state
    request.session[_VERIFIER_KEY] = verifier
    url = google_oauth.build_authorize_url(
        config, redirect_uri=callback_uri(request), state=state, code_challenge=challenge
    )
    return RedirectResponse(url=url, status_code=303)


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Handle Google's redirect: verify state, exchange the code, store the grant."""
    expected_state = request.session.pop(_STATE_KEY, None)
    verifier = request.session.pop(_VERIFIER_KEY, None)

    if error:
        _record_connect_failure(
            request, user, reason=f"consent declined at Google ({error})",
            detail={"google_error": error},
        )
        flash(request, f"Google connection was cancelled ({error}).", "warning")
        return RedirectResponse(url="/ui/tasks", status_code=303)
    if not code or not state or state != expected_state or not verifier:
        _record_connect_failure(
            request, user, reason="invalid or expired authorization response",
            detail={
                "has_code": bool(code),
                "has_state": bool(state),
                "state_matched": bool(state and state == expected_state),
                "has_verifier": bool(verifier),
            },
        )
        flash(request, "Google connection failed (invalid or expired request). Try again.", "error")
        return RedirectResponse(url="/ui/tasks", status_code=303)

    config = google_oauth.get_config(db)
    try:
        tokens = google_oauth.exchange_code(
            config, code=code, redirect_uri=callback_uri(request), code_verifier=verifier
        )
        cred = google_tasks_sync.connect_store(db, user, tokens)
    except Exception as exc:
        log.warning("google_connect_failed", extra={"user": user.username, "error": str(exc)})
        _record_connect_failure(
            request, user, reason="token exchange or credential store failed",
            detail={"error": str(exc), "error_type": type(exc).__name__},
        )
        flash(request, f"Couldn't connect Google Tasks: {exc}", "error")
        return RedirectResponse(url="/ui/tasks", status_code=303)

    # Push the user's existing tasks so "connected" means "all my tasks sync". A
    # backfill hiccup must not fail the connect — the account is already stored and
    # the next scheduled sync will retry — so record it and warn instead of 500ing.
    try:
        synced: int | None = google_tasks_sync.sync_all_for_user(db, user.id)
        backfill_error: str | None = None
    except Exception as exc:
        synced, backfill_error = None, str(exc)
        log.warning(
            "google_backfill_failed", extra={"user": user.username, "error": backfill_error}
        )

    detail: dict = {"surface": "ui", "backfilled": synced}
    if backfill_error:
        detail["backfill_error"] = backfill_error
    record_event(
        category="task",
        event_type="task.google_connected",
        outcome="warning" if backfill_error else "success",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="google_credential", target_id=cred.id, target_label=cred.google_email,
        message=f"Connected Google Tasks ({cred.google_email or 'account'})",
        detail=detail,
        request=request,
    )
    connected_as = f" as {cred.google_email}" if cred.google_email else ""
    if backfill_error:
        flash(
            request,
            f"Google Tasks connected{connected_as}, but the initial sync hit a problem "
            f"({backfill_error}). Your tasks will sync on the next run.",
            "warning",
        )
    else:
        flash(
            request,
            f"Google Tasks connected{connected_as}. "
            f"Synced {synced} task(s) into your “POC Tracker” list.",
            "success",
        )
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.post("/sync")
def sync_now(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """On-demand two-way sync: push local changes, then pull Google-side changes."""
    if not google_tasks_sync.active_credential(db, user.id):
        flash(request, "Connect Google Tasks first.", "error")
        return RedirectResponse(url="/ui/tasks", status_code=303)

    try:
        result = google_tasks_sync.sync_now(db, user.id)
    except Exception as exc:  # never 500 the page — log it and tell the user
        db.rollback()
        log.exception("google_sync_now_failed", extra={"user": user.username})
        record_event(
            category="task", event_type="task.google_sync_failed", outcome="failure",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="google_credential",
            message="Manual Google Tasks sync failed",
            detail={"surface": "ui", "error": str(exc), "error_type": type(exc).__name__},
            request=request,
        )
        flash(request, f"Sync failed: {exc}", "error")
        return RedirectResponse(url="/ui/tasks", status_code=303)

    record_event(
        category="task", event_type="task.google_synced",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="google_credential",
        message="Ran a manual Google Tasks sync",
        detail={
            "surface": "ui", "pulled_updated": result.updated,
            "pulled_created": result.created, "archived": result.archived,
            "completed": result.completed,
        },
        request=request,
    )
    if result.total:
        parts = []
        if result.updated:
            parts.append(f"{result.updated} updated")
        if result.created:
            parts.append(f"{result.created} added from Google")
        if result.archived:
            parts.append(f"{result.archived} archived")
        flash(request, f"Sync complete: {', '.join(parts)}.", "success")
    else:
        flash(request, "Sync complete — everything was already up to date.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)


@router.post("/disconnect")
def disconnect(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    """Disconnect the user's Google account (revoke + drop stored token)."""
    existed = google_tasks_sync.disconnect(db, user.id)
    if existed:
        record_event(
            category="task",
            event_type="task.google_disconnected",
            actor_type="user", actor_label=user.username, actor_id=user.id,
            target_type="google_credential",
            message="Disconnected Google Tasks",
            detail={"surface": "ui"},
            request=request,
        )
        flash(request, "Google Tasks disconnected. Existing Google tasks are left as-is.", "success")
    return RedirectResponse(url="/ui/tasks", status_code=303)
