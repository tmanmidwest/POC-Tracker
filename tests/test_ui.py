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
