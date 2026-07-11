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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from sqlalchemy.orm import Session

from app.db import get_session_factory
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
from app.services.audit import record_event
from app.services.secret_box import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

API_BASE = "https://tasks.googleapis.com/tasks/v1"
LIST_TITLE = "POC Tracker"


def _h(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _raise_for_status(resp: httpx.Response) -> None:
    """Like ``resp.raise_for_status()`` but fold Google's response body into the
    message. Google's REST errors carry the actionable reason in the body (e.g.
    "Tasks API has not been used in project N... enable it by visiting ..." vs
    "Request had insufficient authentication scopes"); the bare status line does
    not. Including it means the connect flash and the activity-log ``detail.error``
    show *why* a call failed, not just "403 Forbidden"."""
    if resp.is_success:
        return
    body = resp.text.strip()
    message = f"{resp.status_code} {resp.reason_phrase} from {resp.request.url}"
    if body:
        message = f"{message}: {body}"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


# ---------------------------------------------------------------------------
# Tasklist + task REST helpers
# ---------------------------------------------------------------------------


def ensure_tasklist(access_token: str) -> str:
    """Find the user's "POC Tracker" list id, creating it if absent."""
    r = client().get(f"{API_BASE}/users/@me/lists", headers=_h(access_token))
    _raise_for_status(r)
    for item in r.json().get("items", []):
        if item.get("title") == LIST_TITLE:
            return str(item["id"])
    r2 = client().post(
        f"{API_BASE}/users/@me/lists", headers=_h(access_token), json={"title": LIST_TITLE}
    )
    _raise_for_status(r2)
    return str(r2.json()["id"])


def _tasks_insert(access: str, tasklist: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = client().post(
        f"{API_BASE}/lists/{tasklist}/tasks", headers=_h(access), json=payload
    )
    _raise_for_status(r)
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
    _raise_for_status(r)
    return r.json()


def _tasks_delete(access: str, tasklist: str, task_id: str) -> None:
    r = client().delete(
        f"{API_BASE}/lists/{tasklist}/tasks/{task_id}", headers=_h(access)
    )
    if r.status_code not in (200, 204, 404):
        _raise_for_status(r)


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
        # Only log on the transition, not on every subsequent push (active_credential
        # returns None once status is needs_reauth, so this fires once per breakage).
        was_connected = cred.status != STATUS_NEEDS_REAUTH
        cred.status = STATUS_NEEDS_REAUTH
        cred.last_error = "Google access was revoked — reconnect to resume syncing."
        db.commit()
        if was_connected:
            record_event(
                category="task",
                event_type="task.google_sync_needs_reauth",
                outcome="failure",
                actor_type="system", actor_label="google-sync",
                target_type="google_credential", target_id=cred.id,
                target_label=cred.google_email,
                message=(
                    f"Google Tasks sync stopped for "
                    f"{cred.google_email or 'a user'} — reconnect required"
                ),
                detail={"reason": "refresh token rejected (revoked or expired)"},
            )
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
        synced_at = datetime.now(UTC)
        task.external_id = str(data.get("id") or task.external_id)
        task.external_etag = data.get("etag")
        task.last_synced_at = synced_at
        # Keep updated_at in lockstep so this sync write isn't later mistaken for a
        # user edit (onupdate would otherwise push updated_at past last_synced_at,
        # making _push_changed re-push the task every sync and clobber remote edits).
        task.updated_at = synced_at
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


# ---------------------------------------------------------------------------
# Pull / reconcile (Google → POC Tracker) — increment 2
# ---------------------------------------------------------------------------


@dataclass
class PullResult:
    """Tally of what a pull changed locally, for flashes and activity detail."""

    updated: int = 0    # existing POC tasks refreshed from Google
    created: int = 0    # tasks created directly in Google, pulled in as new
    archived: int = 0   # tasks deleted in Google → archived locally
    completed: int = 0  # tasks completed in Google → moved to a terminal status

    @property
    def total(self) -> int:
        return self.updated + self.created + self.archived


def _parse_rfc3339(value: str | None) -> datetime | None:
    """Parse a Google RFC3339 timestamp (…Z) into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rfc3339(dt: datetime) -> str:
    """Format an aware datetime as the RFC3339 Google expects for updatedMin."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_due(value: str | None) -> date | None:
    parsed = _parse_rfc3339(value)
    return parsed.date() if parsed else None


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC-aware. SQLite drops tzinfo on read (even for
    DateTime(timezone=True) columns), so timestamps loaded from the DB come back
    naive; comparing them to Google's tz-aware timestamps would raise TypeError.
    All stored timestamps are UTC, so attach UTC to naive values."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _default_open_status(db: Session) -> TaskStatus | None:
    """Lowest-sort active non-terminal status (where a re-opened task lands)."""
    return (
        db.query(TaskStatus)
        .filter(TaskStatus.is_terminal.is_(False), TaskStatus.is_active.is_(True))
        .order_by(TaskStatus.sort_order)
        .first()
    )


def _default_terminal_status(db: Session) -> TaskStatus | None:
    """Lowest-sort active terminal status (where a completed task lands)."""
    return (
        db.query(TaskStatus)
        .filter(TaskStatus.is_terminal.is_(True), TaskStatus.is_active.is_(True))
        .order_by(TaskStatus.sort_order)
        .first()
    )


def _is_terminal(db: Session, status_id: int) -> bool:
    status = db.get(TaskStatus, status_id)
    return bool(status and status.is_terminal)


def _list_remote_tasks(
    access: str, tasklist: str, updated_min: str | None
) -> list[dict[str, Any]]:
    """List every task in the list (paged), including completed/hidden/deleted so we
    can detect completions and deletions. ``updated_min`` limits to recent changes."""
    params: dict[str, str] = {
        "showCompleted": "true",
        "showHidden": "true",
        "showDeleted": "true",
        "maxResults": "100",
    }
    if updated_min:
        params["updatedMin"] = updated_min
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        r = client().get(
            f"{API_BASE}/lists/{tasklist}/tasks", headers=_h(access), params=params
        )
        _raise_for_status(r)
        body = r.json()
        items.extend(body.get("items", []))
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return items


def _apply_remote_to_task(db: Session, task: Task, remote: dict[str, Any]) -> bool:
    """Overwrite a local task's fields from a Google task (remote wins). Returns
    True if the done/not-done state changed (so callers can tally completions)."""
    task.title = (remote.get("title") or task.title)[:300]
    task.details = remote.get("notes") or None
    task.details_html = None  # Google notes are plain text; drop stale rich HTML
    task.due_date = _parse_due(remote.get("due"))

    was_terminal = _is_terminal(db, task.status_id)
    now_completed = remote.get("status") == "completed"
    completion_changed = was_terminal != now_completed
    if completion_changed:
        target = _default_terminal_status(db) if now_completed else _default_open_status(db)
        if target is not None:
            task.status_id = target.id

    task.external_etag = remote.get("etag")
    task.sync_enabled = True
    # Lockstep updated_at with last_synced_at: applying a remote change is not a
    # pending local edit, so it must not look newer than the sync watermark.
    synced_at = datetime.now(UTC)
    task.last_synced_at = synced_at
    task.updated_at = synced_at
    db.flush()
    return completion_changed


def _create_local_from_remote(
    db: Session, cred: UserGoogleCredential, remote: dict[str, Any]
) -> bool:
    """Create a POC task from a Google-origin task. Returns True if it came in done."""
    completed = remote.get("status") == "completed"
    status = _default_terminal_status(db) if completed else _default_open_status(db)
    if status is None:  # a DB with no matching status kind — skip rather than crash
        return False
    task = Task(
        owner_user_id=cred.app_user_id,
        title=(remote.get("title") or "Untitled").strip()[:300] or "Untitled",
        status_id=status.id,
        details=remote.get("notes") or None,
        due_date=_parse_due(remote.get("due")),
        external_id=str(remote["id"]),
        external_etag=remote.get("etag"),
        sync_enabled=True,
    )
    db.add(task)
    synced_at = datetime.now(UTC)
    task.last_synced_at = synced_at
    task.updated_at = synced_at  # lockstep — a freshly-pulled task isn't a local edit
    db.flush()
    return completed


def _archive_from_remote(db: Session, task: Task) -> None:
    """Soft-archive a local task whose Google counterpart was deleted, and unlink it
    so a later restore re-creates a fresh Google task."""
    synced_at = datetime.now(UTC)
    task.is_archived = True
    task.archived_at = synced_at
    task.external_id = None
    task.external_etag = None
    task.last_synced_at = synced_at
    task.updated_at = synced_at  # lockstep — see _apply_remote_to_task
    db.flush()


def _reconcile_one(
    db: Session, cred: UserGoogleCredential, remote: dict[str, Any], result: PullResult
) -> None:
    ext = remote.get("id")
    if not ext:
        return
    task = (
        db.query(Task)
        .filter(Task.owner_user_id == cred.app_user_id, Task.external_id == str(ext))
        .one_or_none()
    )
    remote_updated = _parse_rfc3339(remote.get("updated"))
    is_deleted = bool(remote.get("deleted"))

    if task is None:
        if is_deleted:
            return  # nothing local to reconcile against a deleted remote
        if _create_local_from_remote(db, cred, remote):
            result.completed += 1
        result.created += 1
        return

    # Matched: last-edit-wins. Skip if the remote hasn't changed since we last synced
    # this task, or if the local copy is the newer edit (the push path owns that).
    # Coerce DB timestamps to aware UTC so they compare with Google's aware ones.
    watermark = _ensure_aware(task.last_synced_at)
    local_updated = _ensure_aware(task.updated_at)
    remote_changed = (
        watermark is None or remote_updated is None or remote_updated > watermark
    )
    if not remote_changed:
        return
    local_changed = watermark is None or (
        local_updated is not None and local_updated > watermark
    )
    if local_changed and remote_updated is not None and local_updated is not None:
        if local_updated >= remote_updated:
            return  # local is the newer edit → local wins; leave for push

    if is_deleted:
        _archive_from_remote(db, task)
        result.archived += 1
        return
    if _apply_remote_to_task(db, task, remote):
        result.completed += 1
    result.updated += 1


def reconcile_user(db: Session, user_id: int) -> PullResult:
    """Pull Google-side changes into POC Tracker for one user. Never raises."""
    result = PullResult()
    cred = active_credential(db, user_id)
    if cred is None:
        return result
    try:
        access = _access_token(db, cred)
    except GoogleNeedsReauth:
        return result  # _access_token recorded the needs-reauth state + event

    last_sync = _ensure_aware(cred.last_sync_at)
    updated_min = _rfc3339(last_sync) if last_sync else None
    try:
        remotes = _list_remote_tasks(access, cred.tasklist_id or "", updated_min)
        for remote in remotes:
            _reconcile_one(db, cred, remote, result)
        cred.last_sync_at = datetime.now(UTC)
        cred.last_error = None
        db.commit()
    except Exception as exc:
        db.rollback()
        log.exception("google_pull_failed", extra={"user_id": user_id})
        cred.last_error = str(exc)[:500]
        db.commit()
        record_event(
            category="task", event_type="task.google_pull_failed", outcome="failure",
            actor_type="system", actor_label="google-sync",
            target_type="google_credential", target_id=cred.id,
            target_label=cred.google_email,
            message=f"Google Tasks pull failed for {cred.google_email or 'a user'}",
            detail={"error": str(exc), "error_type": type(exc).__name__},
        )
        return PullResult()
    return result


def _push_changed(db: Session, user_id: int) -> None:
    """Push locally-changed (or never-synced) active tasks — retries failed pushes."""
    cred = active_credential(db, user_id)
    if cred is None:
        return
    tasks = (
        db.query(Task)
        .filter(
            Task.owner_user_id == user_id,
            Task.is_archived.is_(False),
            (Task.last_synced_at.is_(None)) | (Task.updated_at > Task.last_synced_at),
        )
        .all()
    )
    for task in tasks:
        push_task(db, task)
        db.refresh(cred)
        if not cred.is_connected:
            break


def sync_now(db: Session, user_id: int) -> PullResult:
    """Full two-way sync for one user: pull Google-side changes first, then push
    local ones. Pull-first so a remote edit (e.g. a completion) is applied before
    any push, and never clobbered by a stale push in the same run."""
    result = reconcile_user(db, user_id)
    _push_changed(db, user_id)
    return result


def run_pull_sweep() -> int:
    """Sync every connected user in its own session. Never raises. Returns the count
    of users swept. Called on an interval from the app lifespan."""
    db = get_session_factory()()
    try:
        creds = (
            db.query(UserGoogleCredential)
            .filter(UserGoogleCredential.status == STATUS_CONNECTED)
            .all()
        )
        user_ids = [c.app_user_id for c in creds]
        swept = 0
        for user_id in user_ids:
            try:
                sync_now(db, user_id)
                swept += 1
            except Exception:  # one user's failure must not stop the rest
                log.exception("google_sync_sweep_user_failed", extra={"user_id": user_id})
                db.rollback()
        if user_ids:
            log.info("google_sync_sweep", extra={"users": len(user_ids), "swept": swept})
        return swept
    finally:
        db.close()
