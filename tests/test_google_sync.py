"""Phase-2 Google Tasks sync tests (increment 1: push).

A stateful in-memory fake Google backend is wired in via httpx.MockTransport, so
the OAuth token exchange/refresh and the Tasks REST calls run end-to-end with no
network. Covers connect, field mapping, create/update/complete/archive/delete
push, refresh-revoked handling, and that API-driven task changes sync too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import parse_qsl

import httpx
import pytest


def _make_backend() -> tuple[dict, httpx.MockTransport]:
    state: dict = {"lists": {}, "tasks": {}, "counter": 0, "revoked": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        method = request.method

        if url.startswith("https://oauth2.googleapis.com/token"):
            data = dict(parse_qsl(request.content.decode()))
            if data.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": "acc-initial", "refresh_token": "ref-1",
                    "scope": "openid email https://www.googleapis.com/auth/tasks",
                    "expires_in": 3600,
                })
            if data.get("grant_type") == "refresh_token":
                if data.get("refresh_token") in state["revoked"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "acc-live", "expires_in": 3600})

        if url.startswith("https://openidconnect.googleapis.com/v1/userinfo"):
            return httpx.Response(200, json={"email": "user@example.com"})
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            state["revoked"].add("ref-1")
            return httpx.Response(200, json={})

        if "/users/@me/lists" in url:
            if method == "GET":
                items = [{"id": lid, "title": t} for lid, t in state["lists"].items()]
                return httpx.Response(200, json={"items": items})
            if method == "POST":
                body = json.loads(request.content)
                lid = f"list-{len(state['lists']) + 1}"
                state["lists"][lid] = body["title"]
                return httpx.Response(200, json={"id": lid, "title": body["title"]})

        m = re.match(r".*/lists/([^/]+)/tasks/?([^/?]*)$", url)
        if m:
            task_id = m.group(2)
            if method == "POST":
                body = json.loads(request.content)
                tid = f"gt-{state['counter']}"
                state["counter"] += 1
                rec = {**body, "id": tid, "etag": f"etag-{tid}-1"}
                state["tasks"][tid] = rec
                return httpx.Response(200, json=rec)
            if method == "PATCH":
                if task_id not in state["tasks"]:
                    return httpx.Response(404, json={"error": "not found"})
                state["tasks"][task_id].update(json.loads(request.content))
                state["tasks"][task_id]["etag"] = f"etag-{task_id}-2"
                return httpx.Response(200, json=state["tasks"][task_id])
            if method == "DELETE":
                state["tasks"].pop(task_id, None)
                return httpx.Response(204)

        return httpx.Response(500, json={"error": f"unhandled {method} {url}"})

    return state, httpx.MockTransport(handler)


@pytest.fixture
def gbackend(_isolated_data_dir) -> Iterator[dict]:
    """Enabled Google config + a mocked Google backend. Yields the fake state."""
    from app.db import get_session_factory
    from app.services import google_http, google_oauth

    state, transport = _make_backend()
    google_http.set_client(httpx.Client(transport=transport))

    db = get_session_factory()()
    # Seed the app (migrations/seed run lazily only via app startup); ensure tables
    # + seed exist by importing create_app path. The autouse fixture gives a fresh
    # DB; run migrations+seed explicitly here.
    from app.services.migrations import run_migrations
    from app.services.seed_data import seed_database

    run_migrations()
    seed_database(db)
    google_oauth.set_config(db, client_id="cid", client_secret="csecret", is_enabled=True)
    db.close()

    yield state
    google_http.set_client(None)


def _admin(db):  # type: ignore[no-untyped-def]
    from app.models import AppUser

    return db.query(AppUser).filter(AppUser.is_seeded.is_(True)).first()


def _status(db, name):  # type: ignore[no-untyped-def]
    from app.models import TaskStatus

    return db.query(TaskStatus).filter(TaskStatus.name == name).one()


def _connect_admin(db):  # type: ignore[no-untyped-def]
    from app.services import google_oauth, google_tasks_sync

    config = google_oauth.get_config(db)
    tokens = google_oauth.exchange_code(
        config, code="x", redirect_uri="https://app/cb", code_verifier="v"
    )
    return google_tasks_sync.connect_store(db, _admin(db), tokens)


def test_connect_stores_encrypted_token_and_creates_list(gbackend) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.services import google_tasks_sync
    from app.services.secret_box import decrypt_secret

    db = get_session_factory()()
    cred = _connect_admin(db)
    assert cred.status == "connected"
    assert cred.google_email == "user@example.com"
    assert cred.tasklist_id in gbackend["lists"]
    assert gbackend["lists"][cred.tasklist_id] == "POC Tracker"
    # Refresh token stored encrypted (not plaintext), recoverable.
    assert cred.refresh_token_encrypted != "ref-1"
    assert decrypt_secret(cred.refresh_token_encrypted) == "ref-1"
    db.close()


def test_push_create_update_complete_archive(gbackend) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    from app.db import get_session_factory
    from app.models import Task
    from app.services import google_tasks_sync

    db = get_session_factory()()
    _connect_admin(db)
    admin = _admin(db)
    todo = _status(db, "To Do")
    done = _status(db, "Done")

    task = Task(owner_user_id=admin.id, title="Prep env", status_id=todo.id,
                due_date=date(2026, 7, 10), details="call the champion")
    db.add(task)
    db.commit()

    google_tasks_sync.push_task(db, task)
    assert task.external_id and task.external_id in gbackend["tasks"]
    remote = gbackend["tasks"][task.external_id]
    assert remote["title"] == "Prep env"
    assert remote["notes"] == "call the champion"
    assert remote["status"] == "needsAction"
    assert remote["due"].startswith("2026-07-10")
    assert task.external_etag  # stored for concurrency

    # Complete it (terminal status) -> patched to completed on the same remote id.
    gid = task.external_id
    task.status_id = done.id
    google_tasks_sync.sync_after_change(db, task)
    assert task.external_id == gid  # updated in place, not re-created
    assert gbackend["tasks"][gid]["status"] == "completed"

    # Archive -> removed from the Google list and link cleared.
    task.is_archived = True
    google_tasks_sync.sync_after_change(db, task)
    assert gid not in gbackend["tasks"]
    assert task.external_id is None
    db.close()


def test_delete_removes_remote(gbackend) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import Task
    from app.services import google_tasks_sync

    db = get_session_factory()()
    _connect_admin(db)
    admin = _admin(db)
    task = Task(owner_user_id=admin.id, title="Temp", status_id=_status(db, "To Do").id)
    db.add(task)
    db.commit()
    google_tasks_sync.push_task(db, task)
    gid = task.external_id
    assert gid in gbackend["tasks"]

    google_tasks_sync.push_delete(db, admin.id, gid)
    assert gid not in gbackend["tasks"]
    db.close()


def test_no_push_when_not_connected(gbackend) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import Task
    from app.services import google_tasks_sync

    db = get_session_factory()()
    admin = _admin(db)  # never connected
    task = Task(owner_user_id=admin.id, title="Solo", status_id=_status(db, "To Do").id)
    db.add(task)
    db.commit()
    google_tasks_sync.push_task(db, task)
    assert task.external_id is None
    assert not gbackend["tasks"]
    db.close()


def test_revoked_refresh_marks_needs_reauth(gbackend) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.models import Task
    from app.services import google_tasks_sync

    db = get_session_factory()()
    _connect_admin(db)
    admin = _admin(db)
    task = Task(owner_user_id=admin.id, title="Will fail", status_id=_status(db, "To Do").id)
    db.add(task)
    db.commit()

    gbackend["revoked"].add("ref-1")  # simulate user revoking access at Google
    google_tasks_sync.push_task(db, task)

    cred = google_tasks_sync.get_credential(db, admin.id)
    assert cred.status == "needs_reauth"
    assert task.external_id is None  # nothing synced
    db.close()


def test_disconnect_revokes_and_drops(gbackend) -> None:  # type: ignore[no-untyped-def]
    from app.db import get_session_factory
    from app.services import google_tasks_sync

    db = get_session_factory()()
    _connect_admin(db)
    admin = _admin(db)
    assert google_tasks_sync.disconnect(db, admin.id) is True
    assert google_tasks_sync.get_credential(db, admin.id) is None
    assert "ref-1" in gbackend["revoked"]  # revoke was called
    db.close()


def test_api_task_create_pushes_to_google(gbackend, api_client) -> None:  # type: ignore[no-untyped-def]
    """A task created through the REST API syncs for a connected owner."""
    from app.config import get_settings
    from app.db import get_session_factory

    db = get_session_factory()()
    _connect_admin(db)
    db.close()

    owner = get_settings().initial_admin_username
    r = api_client.post("/api/v1/tasks/", json={"owner": owner, "title": "From API"})
    assert r.status_code == 201, r.text
    assert any(t["title"] == "From API" for t in gbackend["tasks"].values())
