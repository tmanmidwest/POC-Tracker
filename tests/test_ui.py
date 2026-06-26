"""End-to-end UI tests for the POC Tracker web app."""

from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def ui(client: TestClient) -> TestClient:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    return client


def _png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_unauthed_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/ui/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_dashboard_renders(ui: TestClient) -> None:
    resp = ui.get("/ui/dashboard")
    assert resp.status_code == 200
    assert "Dashboard" in resp.text


def test_root_redirects_to_dashboard(ui: TestClient) -> None:
    resp = ui.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/ui/dashboard"


# ---------------------------------------------------------------------------
# Customer + project + use-case flow
# ---------------------------------------------------------------------------


def _create_customer(ui: TestClient, name: str) -> int:
    resp = ui.post(
        "/ui/customers/new",
        data={"name": name, "website": "", "notes": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[1])


def _create_project(ui: TestClient, customer_id: int, name: str) -> int:
    resp = ui.post(
        "/ui/projects/new",
        data={
            "customer_id": customer_id, "name": name, "status_id": "",
            "start_date": "", "end_date": "", "sales_engineer_id": "",
            "account_executive": "", "account_executive_email": "", "notes": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_full_project_flow(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import ProjectUseCase

    cid = _create_customer(ui, "Globex")
    pid = _create_project(ui, cid, "Globex POC")

    # Add two library entries, then re-open the picker and add the same two
    # plus one more — only the new one should be added (de-dup).
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1", "2"]}, follow_redirects=False)
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1", "2", "3"]}, follow_redirects=False)

    db = get_session_factory()()
    count = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).count()
    assert count == 3

    # Add a custom (ad-hoc) use case.
    ui.post(f"/ui/projects/{pid}/use-cases",
            data={"category": "Custom", "name": "Client special", "reference_number": "9.1",
                  "description": "", "success_validation": "", "feature_type_id": ""},
            follow_redirects=False)
    count = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).count()
    assert count == 4

    page = ui.get(f"/ui/projects/{pid}")
    assert page.status_code == 200
    assert "Client special" in page.text


def test_salesforce_opp_link(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Project

    cid = _create_customer(ui, "SF Customer")
    # No-scheme URL should be normalized to https://, and render as a tidy link.
    ui.post("/ui/projects/new", data={
        "customer_id": cid, "name": "SF POC", "status_id": "",
        "start_date": "", "end_date": "", "sales_engineer_id": "",
        "account_executive": "", "account_executive_email": "",
        "salesforce_opp_url": "acme.lightning.force.com/opp/1", "notes": "",
    }, follow_redirects=False)
    db = get_session_factory()()
    p = db.query(Project).filter(Project.name == "SF POC").one()
    assert p.salesforce_opp_url == "https://acme.lightning.force.com/opp/1"

    detail = ui.get(f"/ui/projects/{p.id}").text
    assert "Salesforce Opp" in detail and p.salesforce_opp_url in detail
    assert "Salesforce Opp" in ui.get("/ui/dashboard").text

    # A javascript: (or any non-http) scheme is rejected, not stored.
    ui.post(f"/ui/projects/{p.id}/edit", data={
        "customer_id": cid, "name": "SF POC", "status_id": str(p.status_id),
        "start_date": "", "end_date": "", "sales_engineer_id": "",
        "account_executive": "", "account_executive_email": "",
        "salesforce_opp_url": "javascript:alert(1)", "notes": "",
    }, follow_redirects=False)
    db.expire_all()
    assert db.get(Project, p.id).salesforce_opp_url is None


def test_screenshot_upload_and_serve(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import ProjectUseCase, Screenshot

    cid = _create_customer(ui, "Initech")
    pid = _create_project(ui, cid, "Initech POC")
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1"]}, follow_redirects=False)
    db = get_session_factory()()
    uc = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).first()

    resp = ui.post(f"/ui/projects/use-cases/{uc.id}/screenshots",
                   files={"file": ("s.png", _png(), "image/png")},
                   data={"caption": "login"}, follow_redirects=False)
    assert resp.status_code == 303
    shot = db.query(Screenshot).first()
    assert shot is not None
    served = ui.get(f"/ui/projects/screenshots/{shot.id}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"


def test_reject_non_image_screenshot(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import ProjectUseCase, Screenshot

    cid = _create_customer(ui, "Hooli")
    pid = _create_project(ui, cid, "Hooli POC")
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1"]}, follow_redirects=False)
    db = get_session_factory()()
    uc = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).first()
    ui.post(f"/ui/projects/use-cases/{uc.id}/screenshots",
            files={"file": ("notes.txt", b"hello", "text/plain")}, follow_redirects=False)
    assert db.query(Screenshot).count() == 0


def test_project_notes_crud_and_attachments(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import NoteAttachment, ProjectNote

    cid = _create_customer(ui, "Notes Co")
    pid = _create_project(ui, cid, "Notes POC")

    # Add a note with a date and a PDF attachment in one shot.
    resp = ui.post(
        f"/ui/projects/{pid}/notes",
        data={"body": "Kicked off the POC", "note_date": "2026-06-20"},
        files=[("files", ("plan.pdf", b"%PDF-1.4 fake", "application/pdf"))],
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    note = db.query(ProjectNote).filter(ProjectNote.project_id == pid).one()
    assert note.body == "Kicked off the POC"
    assert note.note_date.isoformat() == "2026-06-20"
    assert note.created_by  # stamped with the acting user
    att = db.query(NoteAttachment).filter(NoteAttachment.project_note_id == note.id).one()
    assert att.original_filename == "plan.pdf"
    att_id = att.id

    # The note + attachment link render on the project page.
    page = ui.get(f"/ui/projects/{pid}").text
    assert "Kicked off the POC" in page
    assert f"/ui/projects/note-attachments/{att_id}" in page

    # Attachment is served (inline) for viewing/download.
    served = ui.get(f"/ui/projects/note-attachments/{att_id}")
    assert served.status_code == 200
    assert "inline" in served.headers.get("content-disposition", "")

    # Edit the note's body + date.
    ui.post(f"/ui/projects/notes/{note.id}/edit",
            data={"body": "Updated summary", "note_date": "2026-06-21"},
            follow_redirects=False)
    db.expire_all()
    note = db.get(ProjectNote, note.id)
    assert note.body == "Updated summary"
    assert note.note_date.isoformat() == "2026-06-21"

    # Upload a second attachment to the existing note.
    ui.post(f"/ui/projects/notes/{note.id}/attachments",
            files=[("files", ("doc.docx", b"PK\x03\x04 fake", None))],
            follow_redirects=False)
    assert db.query(NoteAttachment).filter(NoteAttachment.project_note_id == note.id).count() == 2

    # A disallowed file type is rejected (count unchanged).
    ui.post(f"/ui/projects/notes/{note.id}/attachments",
            files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))],
            follow_redirects=False)
    assert db.query(NoteAttachment).filter(NoteAttachment.project_note_id == note.id).count() == 2

    # Remove one attachment.
    ui.post(f"/ui/projects/note-attachments/{att_id}/delete", follow_redirects=False)
    assert db.query(NoteAttachment).filter(NoteAttachment.id == att_id).count() == 0

    # Deleting the note cascades to its remaining attachments.
    ui.post(f"/ui/projects/notes/{note.id}/delete", follow_redirects=False)
    assert db.query(ProjectNote).filter(ProjectNote.project_id == pid).count() == 0
    assert db.query(NoteAttachment).filter(NoteAttachment.project_note_id == note.id).count() == 0


def test_project_report_html_and_zip(ui: TestClient) -> None:
    import io
    import zipfile

    from app.db import get_session_factory
    from app.models import ProjectUseCase

    cid = _create_customer(ui, "Report Co")
    pid = _create_project(ui, cid, "Report POC")
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1"]}, follow_redirects=False)
    db = get_session_factory()()
    uc = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).first()
    ui.post(f"/ui/projects/use-cases/{uc.id}/screenshots",
            files={"file": ("shot.png", _png(), "image/png")}, follow_redirects=False)
    ui.post(f"/ui/projects/{pid}/notes",
            data={"body": "Journal entry one", "note_date": "2026-06-20"},
            files=[("files", ("brief.pdf", b"%PDF-1.4 x", "application/pdf"))],
            follow_redirects=False)

    # Standalone report page: no nav sidebar, shows journal + screenshot + zip button.
    r = ui.get(f"/ui/reports/projects/{pid}")
    assert r.status_code == 200
    assert "sidebar__nav" not in r.text  # navigation bar omitted
    assert "Journal entry one" in r.text
    assert "/ui/projects/screenshots/" in r.text
    assert "Download all (.zip)" in r.text  # has_artifacts -> zip button shown
    assert "artifacts.zip?v=" in r.text  # cache-busted download link
    assert "/pdf?v=" in r.text

    # Zip bundles screenshots + attachments (and the PDF when WeasyPrint is present).
    z = ui.get(f"/ui/reports/projects/{pid}/artifacts.zip")
    assert z.status_code == 200
    assert z.headers["content-type"] == "application/zip"
    assert "no-store" in z.headers.get("cache-control", "")  # never browser-cached
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert any("/screenshots/" in n for n in names), names
    assert any("/attachments/" in n for n in names), names


def test_project_report_pdf(ui: TestClient) -> None:
    pytest.importorskip("weasyprint")  # needs system libs; runs in the container

    cid = _create_customer(ui, "PDF Co")
    pid = _create_project(ui, cid, "PDF POC")
    r = ui.get(f"/ui/reports/projects/{pid}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_use_case_view_prefs(ui: TestClient) -> None:
    import json

    from app.db import get_session_factory
    from app.models import ProjectUseCase, UseCaseStatus, UseCaseViewPref

    cid = _create_customer(ui, "View Co")
    pid = _create_project(ui, cid, "View POC")
    ui.post(f"/ui/projects/{pid}/use-cases/from-library",
            data={"library_ids": ["1", "2"]}, follow_redirects=False)
    db = get_session_factory()()
    ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
    complete = db.query(UseCaseStatus).filter(UseCaseStatus.is_complete_status.is_(True)).first()
    ucs[1].success_validation = "Must pass SSO test"
    db.commit()
    # Mark the first use case complete (leaves one open).
    ui.post(f"/ui/projects/use-cases/{ucs[0].id}/status",
            data={"status_id": complete.id}, follow_redirects=False)

    # Default view: the Success validation field is off, so its value appears
    # only in the (always-present) edit modal textarea — exactly once.
    page = ui.get(f"/ui/projects/{pid}").text
    assert page.count("Must pass SSO test") == 1

    # Enable the Success validation field and filter to not-completed.
    ui.post(f"/ui/projects/{pid}/use-case-view",
            data={"field_success_validation": "1", "field_ref": "1", "status_filter": "open"},
            follow_redirects=False)
    page = ui.get(f"/ui/projects/{pid}").text
    # Now it shows in the visible row too (row + modal = twice).
    assert page.count("Must pass SSO test") == 2
    assert "(1 shown)" in page             # filter applied: only the 1 open UC remains

    # Preference persisted per-user.
    pref = db.query(UseCaseViewPref).one()
    cfg = json.loads(pref.config_json)
    assert "success_validation" in cfg["fields"]
    assert cfg["status_filter"] == "open"


def test_dashboard_status_ordering(ui: TestClient) -> None:
    import json

    from app.db import get_session_factory
    from app.models import DashboardPref, ProjectStatus

    db = get_session_factory()()
    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    assert len(statuses) >= 2
    reordered = [statuses[1], statuses[0], *statuses[2:]]
    order = ",".join(str(s.id) for s in reordered)

    ui.post("/ui/dashboard/preferences",
            data={"col_name": "1", "sort": "updated", "status_order": order,
                  "status_ids": [str(s.id) for s in statuses]},
            follow_redirects=False)

    pref = db.query(DashboardPref).one()
    cfg = json.loads(pref.config_json)
    assert cfg["status_order"][0] == statuses[1].id

    # Dashboard renders the groups in the saved order.
    page = ui.get("/ui/dashboard").text
    assert page.index(statuses[1].name) < page.index(statuses[0].name)


def test_dark_mode_toggle(ui: TestClient) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models import AppUser

    # Default is light, rendered server-side on <html>.
    assert 'data-theme="light"' in ui.get("/ui/dashboard").text

    # Toggle to dark persists to the account and renders on subsequent loads.
    r = ui.post("/ui/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 204
    assert 'data-theme="dark"' in ui.get("/ui/dashboard").text

    username = get_settings().initial_admin_username
    db = get_session_factory()()
    assert db.query(AppUser).filter(AppUser.username == username).one().theme == "dark"

    # Toggling back to light works too.
    ui.post("/ui/theme", data={"theme": "light"}, follow_redirects=False)
    assert 'data-theme="light"' in ui.get("/ui/dashboard").text


# ---------------------------------------------------------------------------
# Lookups + library (admin)
# ---------------------------------------------------------------------------


def test_lookup_pages_render(ui: TestClient) -> None:
    for slug in ("contact-roles", "project-statuses", "feature-types", "use-case-statuses"):
        resp = ui.get(f"/ui/lookups/{slug}")
        assert resp.status_code == 200, slug


def test_create_lookup_row(ui: TestClient) -> None:
    resp = ui.post("/ui/lookups/feature-types/new",
                   data={"name": "PAM-Test", "description": "x", "is_active": "1"},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert "PAM-Test" in ui.get("/ui/lookups/feature-types").text


def test_library_admin_create(ui: TestClient) -> None:
    resp = ui.post("/ui/library/new",
                   data={"category": "Demo", "name": "Library Item X",
                         "default_reference_number": "1.1", "description": "",
                         "success_validation": "", "feature_type_id": ""},
                   follow_redirects=False)
    assert resp.status_code == 303
    assert "Library Item X" in ui.get("/ui/library").text


def test_mcp_token_settings_page_and_rotate(ui: TestClient) -> None:
    from app.services.mcp_token import read_token

    assert ui.get("/ui/settings/mcp").status_code == 200
    assert read_token() is None
    resp = ui.post("/ui/settings/mcp/rotate", follow_redirects=False)
    assert resp.status_code == 303
    # One-time reveal on the next page load, and the token is persisted.
    assert "poct_" in ui.get("/ui/settings/mcp").text
    assert read_token() is not None
    # Clearing revokes + removes it.
    ui.post("/ui/settings/mcp/clear", follow_redirects=False)
    assert read_token() is None


def test_mcp_gateway_token_and_allowed_hosts(ui: TestClient) -> None:
    from app.services import mcp_gateway

    assert ui.get("/ui/settings/mcp").status_code == 200
    assert mcp_gateway.read_gateway_token() is None

    # Generate the inbound gateway token.
    r = ui.post("/ui/settings/mcp/gateway/rotate", follow_redirects=False)
    assert r.status_code == 303
    assert "poctgw_" in ui.get("/ui/settings/mcp").text  # one-time reveal
    assert mcp_gateway.read_gateway_token() is not None

    # Save allowed hosts, then clear them.
    ui.post("/ui/settings/mcp/allowed-hosts",
            data={"allowed_hosts": "mcp.example.com, 10.0.0.5:*"}, follow_redirects=False)
    assert mcp_gateway.read_allowed_hosts() == ["mcp.example.com", "10.0.0.5:*"]
    ui.post("/ui/settings/mcp/allowed-hosts", data={"allowed_hosts": ""}, follow_redirects=False)
    assert mcp_gateway.read_allowed_hosts() == []

    # Clearing the gateway token leaves the endpoint locked down.
    ui.post("/ui/settings/mcp/gateway/clear", follow_redirects=False)
    assert mcp_gateway.read_gateway_token() is None


def test_mcp_key_flagged_and_protected_in_api_keys_list(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.services import mcp_token

    ui.post("/ui/settings/mcp/rotate", follow_redirects=False)
    db = get_session_factory()()
    mcp_id = mcp_token.current_key_id(db)
    assert mcp_id is not None

    page = ui.get("/ui/settings/api-keys").text
    assert "Manage on MCP page" in page  # dedicated key isn't revoke/delete-able here

    # Revoking/deleting the MCP key from the API-keys list is bounced to the MCP page
    # and leaves the token intact.
    r = ui.post(f"/ui/settings/api-keys/{mcp_id}/revoke", follow_redirects=False)
    assert r.headers["location"] == "/ui/settings/mcp"
    r = ui.post(f"/ui/settings/api-keys/{mcp_id}/delete", follow_redirects=False)
    assert r.headers["location"] == "/ui/settings/mcp"
    assert mcp_token.read_token() is not None


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def _make_standard_user(username: str = "stduser") -> None:
    from app.db import get_session_factory
    from app.models import AppUser
    from app.services.passwords import hash_password

    db = get_session_factory()()
    db.add(AppUser(username=username, password_hash=hash_password("password123"),
                   is_active=True, is_admin=False))
    db.commit()


def test_standard_user_blocked_from_admin_areas(client: TestClient) -> None:
    _make_standard_user()
    _login(client, "stduser", "password123")
    assert client.get("/ui/projects/", follow_redirects=False).status_code == 200
    for path in ("/ui/library/", "/ui/lookups/contact-roles", "/ui/settings/admin-users"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ui/dashboard", path


def test_standard_user_can_create_project(client: TestClient) -> None:
    _make_standard_user("stduser2")
    _login(client, "stduser2", "password123")
    cid = _create_customer(client, "StdCo")
    pid = _create_project(client, cid, "StdCo POC")
    assert client.get(f"/ui/projects/{pid}").status_code == 200
