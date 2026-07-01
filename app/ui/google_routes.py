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
        flash(request, f"Google connection was cancelled ({error}).", "warning")
        return RedirectResponse(url="/ui/tasks", status_code=303)
    if not code or not state or state != expected_state or not verifier:
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
        flash(request, "Couldn't connect Google Tasks. Please try again.", "error")
        return RedirectResponse(url="/ui/tasks", status_code=303)

    # Push the user's existing tasks so "connected" means "all my tasks sync".
    synced = google_tasks_sync.sync_all_for_user(db, user.id)

    record_event(
        category="task",
        event_type="task.google_connected",
        actor_type="user", actor_label=user.username, actor_id=user.id,
        target_type="google_credential", target_id=cred.id, target_label=cred.google_email,
        message=f"Connected Google Tasks ({cred.google_email or 'account'})",
        detail={"surface": "ui", "backfilled": synced},
        request=request,
    )
    flash(
        request,
        f"Google Tasks connected{f' as {cred.google_email}' if cred.google_email else ''}. "
        f"Synced {synced} task(s) into your “POC Tracker” list.",
        "success",
    )
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
