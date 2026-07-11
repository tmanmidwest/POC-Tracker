"""Tests for per-customer logo upload, display, normalization, and cleanup."""

from __future__ import annotations

import io

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


def _png_bytes(color: str = "red", size: tuple[int, int] = (40, 24)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size: tuple[int, int] = (1200, 800)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, format="JPEG")
    return buf.getvalue()


def _make_customer(ui: TestClient, name: str) -> int:
    resp = ui.post(
        "/ui/customers/new",
        data={"name": name, "website": "", "notes": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_upload_sets_logo_and_shows_on_pages(ui: TestClient) -> None:
    from app.services import customer_logo

    cid = _make_customer(ui, "Logo Co")
    assert not customer_logo.has_logo(cid)

    resp = ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Logo Co", "website": "", "notes": ""},
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert customer_logo.has_logo(cid)
    uri = customer_logo.data_uri(cid)
    assert uri and uri.startswith("data:image/png;base64,")

    # Customer detail page inlines the logo.
    page = ui.get(f"/ui/customers/{cid}")
    assert uri in page.text


def test_remove_logo(ui: TestClient) -> None:
    from app.services import customer_logo

    cid = _make_customer(ui, "Remove Co")
    ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Remove Co", "website": "", "notes": ""},
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
    )
    assert customer_logo.has_logo(cid)

    ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Remove Co", "website": "", "notes": "", "remove_logo": "1"},
    )
    assert not customer_logo.has_logo(cid)


def test_non_image_rejected(ui: TestClient) -> None:
    from app.services import customer_logo

    cid = _make_customer(ui, "Bad Upload Co")
    resp = ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Bad Upload Co", "website": "", "notes": ""},
        files={"logo": ("logo.png", b"this is not an image", "image/png")},
        follow_redirects=False,
    )
    # Bounces back to the edit form, and nothing is stored.
    assert resp.status_code == 303
    assert "/edit" in resp.headers["location"]
    assert not customer_logo.has_logo(cid)


def test_service_normalizes_to_bounded_png(ui: TestClient) -> None:
    from PIL import Image

    from app.services import customer_logo

    cid = _make_customer(ui, "Normalize Co")
    customer_logo.save(cid, _jpeg_bytes(size=(1200, 800)))  # large JPEG in

    path = customer_logo.path_for(cid)
    assert path.exists()
    img = Image.open(path)
    assert img.format == "PNG"  # re-encoded
    assert max(img.size) <= customer_logo.MAX_EDGE  # downscaled


def test_svg_rejected(ui: TestClient) -> None:
    from app.services import customer_logo

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    with pytest.raises(customer_logo.LogoError):
        customer_logo.save(999, svg)
    assert not customer_logo.has_logo(999)


def test_delete_customer_removes_logo(ui: TestClient) -> None:
    from app.services import customer_logo

    cid = _make_customer(ui, "Doomed Co")
    ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Doomed Co", "website": "", "notes": ""},
        files={"logo": ("logo.png", _png_bytes(), "image/png")},
    )
    assert customer_logo.has_logo(cid)

    resp = ui.post(f"/ui/customers/{cid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert not customer_logo.has_logo(cid)


def test_logo_appears_on_public_portal(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Customer, Project, ProjectStatus, ProjectShareLink
    from app.services import customer_logo

    cid = _make_customer(ui, "Portal Logo Co")
    ui.post(
        f"/ui/customers/{cid}/edit",
        data={"name": "Portal Logo Co", "website": "", "notes": ""},
        files={"logo": ("logo.png", _png_bytes(color="green"), "image/png")},
    )
    # Create a project for this customer + enable its portal link.
    db = get_session_factory()()
    try:
        status = db.query(ProjectStatus).first()
        project = Project(customer_id=cid, name="Portal Logo POC", status_id=status.id)
        db.add(project)
        db.commit()
        pid = project.id
    finally:
        db.close()
    ui.post(f"/ui/projects/{pid}/share-link/enable")

    db = get_session_factory()()
    try:
        token = db.query(ProjectShareLink).filter_by(project_id=pid).one().token
    finally:
        db.close()

    page = ui.get(f"/portal/{token}")
    assert page.status_code == 200
    assert customer_logo.data_uri(cid) in page.text
