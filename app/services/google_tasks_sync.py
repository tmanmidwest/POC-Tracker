"""Google Tasks sync — increment 1: push (POC Tracker → Google).

Each connected user has a dedicated "POC Tracker" Google Tasks list. Task
create/update/complete pushes to that list; delete removes the remote task. All
push calls are best-effort: a Google failure never breaks a save — it records
``last_error`` on the credential for the reconcile pass (increment 2) to retry.

Field mapping (Google Tasks is intentionally thin):
    title    -> title
    details  -> notes         (plain text; Google notes carry no HTML)
    due_date -> due           (date at midnight UTC, RFC3339)
    status   -> needsAction / completed   (completed when the status is terminal)
start_date, priority, and project have no Google Tasks equivalent.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import Task, TaskStatus
from app.models.app_user import AppUser
from app.models.user_google_credential import (
    STATUS_CONNECTED,
    STATUS_NEEDS_REAUTH,
    UserGoogleCredential,
)
from app.services.google_http import client
from app.services.google_oauth import (
    SCOPES,
    GoogleNeedsReauth,
    GoogleNotConfigured,
    fetch_email,
    get_config,
    is_ready,
    refresh_access_token,
    revoke,
)
from app.services.secret_box import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

API_BASE = "https://tasks.googleapis.com/tasks/v1"
LIST_TITLE = "POC Tracker"


def _h(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# ---------------------------------------------------------------------------
# Tasklist + task REST helpers
# ---------------------------------------------------------------------------


def ensure_tasklist(access_token: str) -> str:
    """Find the user's "POC Tracker" list id, creating it if absent."""
    r = client().get(f"{API_BASE}/users/@me/lists", headers=_h(access_token))
    r.raise_for_status()
    for item in r.json().get("items", []):
        if item.get("title") == LIST_TITLE:
            return str(item["id"])
    r2 = client().post(
        f"{API_BASE}/users/@me/lists", headers=_h(access_token), json={"title": LIST_TITLE}
    )
    r2.raise_for_status()
    return str(r2.json()["id"])


def _tasks_insert(access: str, tasklist: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = client().post(
        f"{API_BASE}/lists/{tasklist}/tasks", headers=_h(access), json=payload
    )
    r.raise_for_status()
    return r.json()


def _tasks_patch(
    access: str, tasklist: str, task_id: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """PATCH a remote task; returns None if it no longer exists (404)."""
    r = client().patch(
        f"{API_BASE}/lists/{tasklist}/tasks/{task_id}", headers=_h(access), json=payload
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _tasks_delete(access: str, tasklist: str, task_id: str) -> None:
    r = client().delete(
        f"{API_BASE}/lists/{tasklist}/tasks/{task_id}", headers=_h(access)
    )
    if r.status_code not in (200, 204, 404):
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def _task_payload(db: Session, task: Task) -> dict[str, Any]:
    """Build the Google Tasks body from a POC task.

    The terminal flag is read fresh by status_id rather than the cached
    ``task.status`` relationship, which can be stale after a status change
    (the session uses expire_on_commit=False).
    """
    status = db.get(TaskStatus, task.status_id)
    is_done = bool(status and status.is_terminal)
    payload: dict[str, Any] = {
        "title": task.title,
        "notes": task.details or "",
        "status": "completed" if is_done else "needsAction",
    }
    if task.due_date is not None:
        payload["due"] = f"{task.due_date.isoformat()}T00:00:00.000Z"
    return payload


# ---------------------------------------------------------------------------
# Credential access
# ---------------------------------------------------------------------------


def get_credential(db: Session, user_id: int) -> UserGoogleCredential | None:
    return (
        db.query(UserGoogleCredential)
        .filter(UserGoogleCredential.app_user_id == user_id)
        .one_or_none()
    )


def active_credential(db: Session, user_id: int) -> UserGoogleCredential | None:
    """The user's credential if it's connected and ready to sync, else None."""
    if not is_ready(db):
        return None
    cred = get_credential(db, user_id)
    if cred is None or not cred.is_connected or not cred.tasklist_id:
        return None
    return cred


def _access_token(db: Session, cred: UserGoogleCredential) -> str:
    """Mint an access token, flipping the credential to needs_reauth on rejection."""
    config = get_config(db)
    refresh = decrypt_secret(cred.refresh_token_encrypted)
    try:
        return refresh_access_token(config, refresh)
    except GoogleNeedsReauth:
        cred.status = STATUS_NEEDS_REAUTH
        cred.last_error = "Google access was revoked — reconnect to resume syncing."
        db.commit()
        raise


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


def connect_store(db: Session, user: AppUser, token_response: dict[str, Any]) -> UserGoogleCredential:
    """Persist a new/updated connection from an OAuth token response."""
    refresh = token_response.get("refresh_token")
    access = token_response.get("access_token")
    if not refresh or not access:
        raise GoogleNotConfigured(
            "Google did not return a refresh token. Disconnect any prior grant in "
            "your Google account and reconnect."
        )
    email = fetch_email(access)
    tasklist_id = ensure_tasklist(access)

    cred = get_credential(db, user.id) or UserGoogleCredential(app_user_id=user.id)
    cred.refresh_token_encrypted = encrypt_secret(refresh)
    cred.scopes = token_response.get("scope", SCOPES)
    cred.google_email = email
    cred.tasklist_id = tasklist_id
    cred.status = STATUS_CONNECTED
    cred.connected_at = datetime.now(UTC)
    cred.last_error = None
    db.add(cred)
    db.commit()
    db.refresh(cred)
    return cred


def disconnect(db: Session, user_id: int) -> bool:
    """Revoke and remove a user's Google connection. Returns True if one existed."""
    cred = get_credential(db, user_id)
    if cred is None:
        return False
    try:
        revoke(decrypt_secret(cred.refresh_token_encrypted))
    except Exception:  # best-effort; still drop the local record
        pass
    db.delete(cred)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push_task(db: Session, task: Task) -> None:
    """Best-effort push of a single task to Google. Never raises."""
    cred = active_credential(db, task.owner_user_id)
    if cred is None:
        return
    try:
        access = _access_token(db, cred)
        payload = _task_payload(db, task)
        data: dict[str, Any] | None = None
        if task.external_id:
            data = _tasks_patch(access, cred.tasklist_id, task.external_id, payload)
        if data is None:  # never synced, or the remote task was deleted → (re)create
            data = _tasks_insert(access, cred.tasklist_id, payload)
        task.external_id = str(data.get("id") or task.external_id)
        task.external_etag = data.get("etag")
        task.last_synced_at = datetime.now(UTC)
        task.sync_enabled = True
        cred.last_error = None
        db.commit()
    except GoogleNeedsReauth:
        pass  # _access_token already recorded the state
    except Exception as exc:  # leave for the reconcile pass to retry
        log.warning("google_push_failed", extra={"task_id": task.id, "error": str(exc)})
        cred.last_error = str(exc)[:500]
        db.commit()


def sync_after_change(db: Session, task: Task) -> None:
    """Reflect a create/update/status/archive change to Google. Never raises.

    Active tasks are upserted; archiving removes the task from the Google list
    (and clears its link so a later restore re-creates it).
    """
    if task.is_archived:
        if task.external_id:
            ext = task.external_id
            push_delete(db, task.owner_user_id, ext)
            task.external_id = None
            task.external_etag = None
            db.commit()
        return
    push_task(db, task)


def push_delete(db: Session, owner_user_id: int, external_id: str | None) -> None:
    """Best-effort remote delete for a task being deleted locally. Never raises."""
    if not external_id:
        return
    cred = active_credential(db, owner_user_id)
    if cred is None:
        return
    try:
        access = _access_token(db, cred)
        _tasks_delete(access, cred.tasklist_id, external_id)
    except GoogleNeedsReauth:
        pass
    except Exception as exc:
        log.warning("google_delete_failed", extra={"external_id": external_id, "error": str(exc)})


def sync_all_for_user(db: Session, user_id: int) -> int:
    """Push all of a user's active tasks (used right after they connect).

    Best-effort; returns the number attempted. Stops early if the connection
    drops to needs_reauth.
    """
    cred = active_credential(db, user_id)
    if cred is None:
        return 0
    tasks = (
        db.query(Task)
        .filter(Task.owner_user_id == user_id, Task.is_archived.is_(False))
        .all()
    )
    for task in tasks:
        push_task(db, task)
        db.refresh(cred)
        if not cred.is_connected:
            break
    return len(tasks)
