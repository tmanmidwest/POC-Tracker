"""REST API tests for project journal notes."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _a_project_id(api_client: TestClient) -> int:
    return int(api_client.get("/api/v1/projects/").json()[0]["id"])


def test_note_crud_lifecycle(api_client: TestClient) -> None:
    pid = _a_project_id(api_client)

    # Create — HTML is sanitized, plain text derived, defaults applied.
    made = api_client.post(
        f"/api/v1/projects/{pid}/notes",
        json={"body": "<p>Kicked off <strong>SSO</strong></p><script>bad()</script>"},
    )
    assert made.status_code == 201, made.text
    note = made.json()
    nid = note["id"]
    assert note["project_id"] == pid
    assert note["is_internal_only"] is False
    assert "<strong>" in note["body_html"] and "<script>" not in note["body_html"]
    assert "SSO" in note["body"]
    assert note["created_by"].startswith("api_key:")  # attributed to the caller
    assert note["note_date"]  # defaulted to today

    # List includes it.
    listed = api_client.get(f"/api/v1/projects/{pid}/notes").json()
    assert any(n["id"] == nid for n in listed)

    # Get one.
    assert api_client.get(f"/api/v1/projects/notes/{nid}").json()["id"] == nid

    # Patch body + flag + date.
    upd = api_client.patch(
        f"/api/v1/projects/notes/{nid}",
        json={"body": "<p>Updated</p>", "is_internal_only": True, "note_date": "2026-02-03"},
    ).json()
    assert "Updated" in upd["body"]
    assert upd["is_internal_only"] is True
    assert upd["note_date"] == "2026-02-03"

    # Delete → gone.
    assert api_client.delete(f"/api/v1/projects/notes/{nid}").status_code == 204
    assert api_client.get(f"/api/v1/projects/notes/{nid}").status_code == 404


def test_note_create_honors_explicit_fields(api_client: TestClient) -> None:
    pid = _a_project_id(api_client)
    made = api_client.post(
        f"/api/v1/projects/{pid}/notes",
        json={
            "body": "<p>Internal only</p>",
            "is_internal_only": True,
            "created_by": "Robby",
            "note_date": "2026-01-15",
        },
    )
    assert made.status_code == 201
    n = made.json()
    assert n["is_internal_only"] is True
    assert n["created_by"] == "Robby"
    assert n["note_date"] == "2026-01-15"


def test_note_empty_body_is_422(api_client: TestClient) -> None:
    pid = _a_project_id(api_client)
    # Only a script tag → sanitizes to no text.
    resp = api_client.post(
        f"/api/v1/projects/{pid}/notes", json={"body": "<script>bad()</script>"}
    )
    assert resp.status_code == 422


def test_note_on_unknown_project_is_404(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/projects/999999/notes", json={"body": "<p>x</p>"}
    )
    assert resp.status_code == 404


def test_notes_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/projects/1/notes").status_code == 401
