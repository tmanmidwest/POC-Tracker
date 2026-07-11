"""Tests for the MCP server tools.

The MCP tools call the REST API over httpx. We inject a TestClient (whose
base_url carries the /api/v1 prefix) as the MCP session so the tools exercise
the real app end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from app import mcp_server
    from app.db import get_session_factory
    from app.main import create_app
    from app.models import ApiKey, AppUser
    from app.services.tokens import generate_api_key, hash_token

    app = create_app()
    with TestClient(app, base_url="http://testserver/api/v1") as tc:
        # Mint an API key directly and attach it to the client.
        full, prefix = generate_api_key()
        db = get_session_factory()()
        admin = db.query(AppUser).first()
        db.add(ApiKey(name="mcp-test", key_prefix=prefix, key_hash=hash_token(full),
                      created_by_user_id=admin.id))
        db.commit()
        tc.headers.update({"Authorization": f"Bearer {full}"})
        monkeypatch.setattr(mcp_server, "_session", tc)
        yield mcp_server


def test_bulk_add_custom_use_cases(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    # Seeded sample project is id 1.
    before = len(m.get_project(1)["use_cases"])
    result = m.add_custom_use_cases(1, [
        {"name": "Bulk import access", "category": "Joiner", "reference_number": "1.5",
         "feature_type": "JML", "status": "Pending Testing"},
        {"name": "SoD policy check", "category": "Certifications",
         "description": "Validate separation-of-duties detection"},
        {"category": "Broken", "description": "missing name -> error"},  # should error
    ])
    assert result["added"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 2
    after = m.get_project(1)["use_cases"]
    assert len(after) == before + 2
    names = {u["name"] for u in after}
    assert "Bulk import access" in names
    # name -> id resolution worked (feature type JML attached)
    bulk = next(u for u in after if u["name"] == "Bulk import access")
    assert bulk["feature_type"]["name"] == "JML"
    assert bulk["source"] == "custom"


def test_single_add_and_status_by_name(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    uc = m.add_custom_use_case(1, name="Quick check", category="Misc")
    completed = m.set_use_case_status(uc["id"], "Completed")
    assert completed["status"]["name"] == "Completed"


def test_update_use_case(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    uc = m.add_custom_use_case(1, name="To edit", category="Misc")
    updated = m.update_use_case(uc["id"], comments="done in demo", reference_number="2.2")
    assert updated["comments"] == "done in demo"
    assert updated["reference_number"] == "2.2"


def test_add_from_library_via_mcp(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    cust = m.create_customer("MCP Customer")
    proj = m.create_project(cust["id"], name="MCP POC", status="Pending Scheduling")
    lib = m.list_use_case_library()
    added = m.add_use_cases_from_library(proj["id"], [lib[0]["id"], lib[1]["id"]])
    assert len(added) == 2
    # Re-adding is de-duplicated.
    again = m.add_use_cases_from_library(proj["id"], [lib[0]["id"], lib[1]["id"]])
    assert len(again) == 0


def test_find_projects(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    found = m.find_projects("acme")
    assert any("acme" in (p["customer"]["name"].lower()) for p in found)


def test_unknown_status_name_is_a_clear_error(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    result = m.add_custom_use_cases(1, [
        {"name": "Bad status", "category": "X", "status": "Nope"},
    ])
    assert result["added"] == 0
    assert "Unknown use-case status" in result["errors"][0]["error"]


def test_task_tools_via_mcp(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    from app.config import get_settings

    owner = get_settings().initial_admin_username

    # Lookups now surface task statuses/priorities by name.
    lookups = m.list_lookups()
    assert "task_statuses" in lookups and "task_priorities" in lookups
    assert any(s["name"] == "In Progress" for s in lookups["task_statuses"])

    # Create a task on the seeded sample project (id 1), resolving names.
    task = m.create_task(
        owner=owner, title="Prep demo env", status="To Do", priority="High",
        project_id=1, due_date="2026-07-15", details="<p>Spin up sandbox</p>",
        is_internal_only=True,
    )
    assert task["owner"]["username"] == owner
    assert task["status"]["name"] == "To Do"
    assert task["priority"]["name"] == "High"
    assert task["project"]["id"] == 1
    # The internal-only flag is set on create and can be toggled off on update.
    assert task["is_internal_only"] is True
    assert m.update_task(task["id"], is_internal_only=False)["is_internal_only"] is False

    # It shows up when listing by owner.
    listed = m.list_tasks(owner=owner)
    assert any(t["id"] == task["id"] for t in listed)

    # Status change + get by name resolution.
    moved = m.set_task_status(task["id"], "In Progress")
    assert moved["status"]["name"] == "In Progress"
    assert m.get_task(task["id"])["status"]["name"] == "In Progress"

    # Update fields, then delete.
    upd = m.update_task(task["id"], title="Prep demo environment", priority="Urgent")
    assert upd["title"] == "Prep demo environment"
    assert upd["priority"]["name"] == "Urgent"
    assert m.delete_task(task["id"])["deleted"] is True


def test_note_tools_via_mcp(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env

    # Add a note to the seeded sample project (id 1); HTML is sanitized.
    note = m.add_note(
        project_id=1,
        body="<p>Kickoff <strong>call</strong> done</p><script>bad()</script>",
        is_internal_only=True,
    )
    assert note["project_id"] == 1
    assert note["is_internal_only"] is True
    assert "call" in note["body"] and "<script>" not in (note["body_html"] or "")
    nid = note["id"]

    # List + get.
    assert any(n["id"] == nid for n in m.list_notes(1))
    assert m.get_note(nid)["id"] == nid

    # Update fields.
    upd = m.update_note(nid, body="<p>Revised</p>", is_internal_only=False, note_date="2026-02-03")
    assert "Revised" in upd["body"]
    assert upd["is_internal_only"] is False
    assert upd["note_date"] == "2026-02-03"

    # The report now includes the journal note.
    assert "Journal notes" in m.project_report(1)

    # Delete.
    assert m.delete_note(nid)["deleted"] is True


def test_task_unknown_owner_is_clear_error(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    try:
        m.create_task(owner="ghost", title="orphan")
    except Exception as exc:  # RuntimeError surfaced from the REST 422
        assert "owner" in str(exc).lower()
    else:
        raise AssertionError("expected an unknown-owner error")


_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "gw", "version": "1"}},
}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


def test_http_gateway_auth_lifecycle(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Inbound access control read live from the UI-managed config:

    unconfigured → 503, missing/wrong bearer → 401, correct → 200, and a Host
    not in a configured allow-list → 403. (One app/client because the streamable
    session manager can only start once per process; the middleware reads live.)
    """
    from app import mcp_server
    from app.db import get_session_factory
    from app.models import AppUser
    from app.services import mcp_gateway, mcp_gateway_tokens

    monkeypatch.delenv("POCT_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("POCT_MCP_ALLOWED_HOSTS", raising=False)

    db = get_session_factory()()
    admin = db.query(AppUser).first()

    app = mcp_server.build_http_app("streamable-http")
    with TestClient(app) as c:
        def post(token: str | None) -> int:
            h = dict(_ACCEPT)
            if token is not None:
                h["Authorization"] = f"Bearer {token}"
            return c.post("/mcp", json=_INIT, headers=h).status_code

        # Deploy-time state: no tokens configured → rejected.
        assert post("anything") == 503

        # Issue a named gateway token in the UI (here, the service it calls).
        row, token = mcp_gateway_tokens.create(db, name="Saviynt", actor_id=admin.id)
        assert post(None) == 401
        assert post("nope") == 401
        assert post(token) == 200

        # A second token authenticates independently of the first.
        _, token2 = mcp_gateway_tokens.create(db, name="Project Atlas", actor_id=admin.id)
        assert post(token2) == 200

        # Revoking one token kills only that token; the other still works.
        mcp_gateway_tokens.revoke(db, row.id)
        assert post(token) == 401
        assert post(token2) == 200

        # A configured allow-list that excludes the request Host → 403.
        mcp_gateway.set_allowed_hosts("only.example.com")
        assert post(token2) == 403
        # Clearing it (empty = any host) opens it back up.
        mcp_gateway.set_allowed_hosts("")
        assert post(token2) == 200


def test_token_rotation_is_read_live(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The MCP server resolves the UI-managed token freshly, and rotating it in
    the app revokes the old one and is picked up without a restart."""
    from app import mcp_server
    from app.db import get_session_factory
    from app.models import ApiKey, AppUser
    from app.services import mcp_token
    from app.services.tokens import hash_token

    monkeypatch.setattr(mcp_server, "API_KEY", "")  # no fixed override
    monkeypatch.setattr(mcp_server, "_session", None)  # use real resolution path

    db = get_session_factory()()
    admin = db.query(AppUser).first()

    assert mcp_server._resolve_token() is None  # nothing configured yet

    t1 = mcp_token.rotate(db, actor_id=admin.id)
    assert mcp_server._resolve_token() == t1

    t2 = mcp_token.rotate(db, actor_id=admin.id)
    assert t2 != t1
    assert mcp_server._resolve_token() == t2  # picked up live

    db.expire_all()
    old = db.query(ApiKey).filter(ApiKey.key_hash == hash_token(t1)).one()
    assert old.revoked_at is not None  # previous token revoked
