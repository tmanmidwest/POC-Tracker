"""REST API tests for the Task Manager (admin-wide, explicit-owner model)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _admin_username(api_client: TestClient) -> str:
    from app.config import get_settings

    return get_settings().initial_admin_username


def test_task_status_and_priority_lookups(api_client: TestClient) -> None:
    statuses = api_client.get("/api/v1/task-statuses/").json()
    names = {s["name"] for s in statuses}
    assert {"To Do", "In Progress", "Blocked", "Done"} <= names
    prios = api_client.get("/api/v1/task-priorities/").json()
    assert {"Low", "Medium", "High", "Urgent"} <= {p["name"] for p in prios}
    # Priority carries a color; status carries the terminal flag.
    assert any(p.get("color") for p in prios)
    assert any(s["is_terminal"] for s in statuses)


def test_create_task_resolves_names(api_client: TestClient) -> None:
    owner = _admin_username(api_client)
    r = api_client.post(
        "/api/v1/tasks/",
        json={
            "owner": owner,
            "title": "Follow up on SSO",
            "status": "In Progress",   # by name
            "priority": "High",         # by name
            "start_date": "2026-07-01",
            "due_date": "2026-07-10",
            "details": "<p>Call the <strong>champion</strong>.</p><script>bad()</script>",
        },
    )
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["title"] == "Follow up on SSO"
    assert t["status"]["name"] == "In Progress"
    assert t["priority"]["name"] == "High"
    assert t["owner"]["username"] == owner
    assert t["start_date"] == "2026-07-01"
    # HTML sanitized (script stripped) and plain text derived.
    assert "<strong>" in t["details_html"] and "<script>" not in t["details_html"]
    assert "champion" in t["details"]


def test_create_task_defaults_status_and_links_project(api_client: TestClient) -> None:
    owner = _admin_username(api_client)
    proj = api_client.get("/api/v1/projects/").json()[0]
    r = api_client.post(
        "/api/v1/tasks/",
        json={"owner": owner, "title": "Task on project", "project_id": proj["id"]},
    )
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["status"]["name"] == "To Do"  # first active status
    assert t["project"]["id"] == proj["id"]
    assert t["priority"] is None


def test_list_filter_get_update_delete(api_client: TestClient) -> None:
    owner = _admin_username(api_client)
    made = api_client.post(
        "/api/v1/tasks/", json={"owner": owner, "title": "Lifecycle task"}
    ).json()
    tid = made["id"]

    # Filter by owner
    listed = api_client.get(f"/api/v1/tasks/?owner={owner}").json()
    assert any(t["id"] == tid for t in listed)

    # Get one
    assert api_client.get(f"/api/v1/tasks/{tid}").json()["title"] == "Lifecycle task"

    # Patch: change status by name + set priority + retitle
    upd = api_client.patch(
        f"/api/v1/tasks/{tid}",
        json={"title": "Renamed", "status": "Done", "priority": "Low"},
    ).json()
    assert upd["title"] == "Renamed"
    assert upd["status"]["name"] == "Done"
    assert upd["priority"]["name"] == "Low"

    # Clear the priority explicitly (null)
    cleared = api_client.patch(f"/api/v1/tasks/{tid}", json={"priority": None}).json()
    assert cleared["priority"] is None

    # Archive via patch
    arch = api_client.patch(f"/api/v1/tasks/{tid}", json={"is_archived": True}).json()
    assert arch["is_archived"] is True and arch["archived_at"] is not None
    # Archived tasks are hidden from the default list but visible with the flag.
    assert all(t["id"] != tid for t in api_client.get("/api/v1/tasks/").json())
    assert any(
        t["id"] == tid
        for t in api_client.get("/api/v1/tasks/?include_archived=true").json()
    )

    # Delete
    assert api_client.delete(f"/api/v1/tasks/{tid}").status_code == 204
    assert api_client.get(f"/api/v1/tasks/{tid}").status_code == 404


def test_internal_only_flag_create_and_update(api_client: TestClient) -> None:
    owner = _admin_username(api_client)

    # Defaults to visible when omitted.
    default = api_client.post(
        "/api/v1/tasks/", json={"owner": owner, "title": "Default vis"}
    ).json()
    assert default["is_internal_only"] is False

    # Settable at create time and round-trips on read.
    made = api_client.post(
        "/api/v1/tasks/",
        json={"owner": owner, "title": "Hidden task", "is_internal_only": True},
    )
    assert made.status_code == 201, made.text
    tid = made.json()["id"]
    assert made.json()["is_internal_only"] is True
    assert api_client.get(f"/api/v1/tasks/{tid}").json()["is_internal_only"] is True

    # Toggleable via patch, both directions.
    off = api_client.patch(
        f"/api/v1/tasks/{tid}", json={"is_internal_only": False}
    ).json()
    assert off["is_internal_only"] is False
    on = api_client.patch(
        f"/api/v1/tasks/{tid}", json={"is_internal_only": True}
    ).json()
    assert on["is_internal_only"] is True

    # A patch that omits the flag leaves it unchanged.
    untouched = api_client.patch(f"/api/v1/tasks/{tid}", json={"title": "Renamed"}).json()
    assert untouched["is_internal_only"] is True


def test_unknown_owner_and_status_are_clear_errors(api_client: TestClient) -> None:
    r = api_client.post("/api/v1/tasks/", json={"owner": "nobody", "title": "x"})
    assert r.status_code == 422 and "owner" in r.json()["detail"].lower()

    owner = _admin_username(api_client)
    r = api_client.post(
        "/api/v1/tasks/", json={"owner": owner, "title": "x", "status": "Nope"}
    )
    assert r.status_code == 422 and "status" in r.json()["detail"].lower()


def test_task_status_in_use_blocks_delete(api_client: TestClient) -> None:
    owner = _admin_username(api_client)
    # Create a deletable (non-system) status, use it, then try to delete it.
    st = api_client.post(
        "/api/v1/task-statuses/", json={"name": "Waiting", "sort_order": 25}
    ).json()
    api_client.post(
        "/api/v1/tasks/", json={"owner": owner, "title": "uses status", "status": "Waiting"}
    )
    assert api_client.delete(f"/api/v1/task-statuses/{st['id']}").status_code == 409


def test_tasks_404_when_module_disabled(api_client: TestClient) -> None:
    from app.db import get_session_factory
    from app.services import system_config

    db = get_session_factory()()
    system_config.set_tasks_enabled(db, False)
    try:
        assert api_client.get("/api/v1/tasks/").status_code == 404
    finally:
        system_config.set_tasks_enabled(db, True)
        db.close()
    assert api_client.get("/api/v1/tasks/").status_code == 200


def test_tasks_require_auth(client: TestClient) -> None:
    # No bearer token -> 401 (auth runs before the module check).
    assert client.get("/api/v1/tasks/").status_code == 401
