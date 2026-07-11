"""Tests for the public customer portal (share links + status page)."""

from __future__ import annotations

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


def _make_project(name: str) -> int:
    """Create a customer + project with two use cases and two notes directly."""
    from app.db import get_session_factory
    from app.models import (
        Customer,
        Project,
        ProjectNote,
        ProjectStatus,
        ProjectUseCase,
        UseCaseStatus,
    )
    from datetime import date

    db = get_session_factory()()
    try:
        customer = Customer(name=f"Cust {name}")
        db.add(customer)
        db.flush()
        status = db.query(ProjectStatus).first()
        project = Project(customer_id=customer.id, name=name, status_id=status.id)
        db.add(project)
        db.flush()

        done = db.query(UseCaseStatus).filter_by(is_complete_status=True).first()
        todo = db.query(UseCaseStatus).filter_by(is_complete_status=False).first()
        db.add(ProjectUseCase(
            project_id=project.id, source="custom", category="Joiner",
            reference_number="1.1", name="Provision new hire", status_id=done.id,
        ))
        db.add(ProjectUseCase(
            project_id=project.id, source="custom", category="Joiner",
            reference_number="1.2", name="Birthright roles", status_id=todo.id,
        ))
        db.add(ProjectNote(
            project_id=project.id, note_date=date(2026, 7, 1),
            body="SHARED UPDATE customer visible", is_internal_only=False,
        ))
        db.add(ProjectNote(
            project_id=project.id, note_date=date(2026, 7, 2),
            body="INTERNAL SECRET do not leak", is_internal_only=True,
        ))
        db.commit()
        return project.id
    finally:
        db.close()


def _token_for(project_id: int) -> str:
    from app.db import get_session_factory
    from app.models import ProjectShareLink

    db = get_session_factory()()
    try:
        link = db.query(ProjectShareLink).filter_by(project_id=project_id).one()
        return link.token
    finally:
        db.close()


def test_enable_creates_live_public_page(ui: TestClient) -> None:
    pid = _make_project("Portal POC")
    resp = ui.post(f"/ui/projects/{pid}/share-link/enable", follow_redirects=False)
    assert resp.status_code == 303

    token = _token_for(pid)
    # Public page needs NO auth — use a bare client.
    from app.main import create_app

    public = TestClient(create_app())
    page = public.get(f"/portal/{token}")
    assert page.status_code == 200
    assert "Portal POC" in page.text
    assert "Provision new hire" in page.text


def test_public_page_hides_internal_notes(ui: TestClient) -> None:
    pid = _make_project("Filter POC")
    ui.post(f"/ui/projects/{pid}/share-link/enable", follow_redirects=False)
    token = _token_for(pid)

    page = ui.get(f"/portal/{token}")
    assert "SHARED UPDATE customer visible" in page.text
    assert "INTERNAL SECRET do not leak" not in page.text


def test_disable_then_reenable_same_token(ui: TestClient) -> None:
    pid = _make_project("Toggle POC")
    ui.post(f"/ui/projects/{pid}/share-link/enable")
    token = _token_for(pid)
    assert ui.get(f"/portal/{token}").status_code == 200

    ui.post(f"/ui/projects/{pid}/share-link/disable")
    assert ui.get(f"/portal/{token}", follow_redirects=False).status_code == 404

    ui.post(f"/ui/projects/{pid}/share-link/enable")
    # Same token comes back to life.
    assert _token_for(pid) == token
    assert ui.get(f"/portal/{token}").status_code == 200


def test_rotate_kills_old_link(ui: TestClient) -> None:
    pid = _make_project("Rotate POC")
    ui.post(f"/ui/projects/{pid}/share-link/enable")
    old = _token_for(pid)

    ui.post(f"/ui/projects/{pid}/share-link/rotate")
    new = _token_for(pid)
    assert new != old
    assert ui.get(f"/portal/{old}", follow_redirects=False).status_code == 404
    assert ui.get(f"/portal/{new}").status_code == 200


def test_unknown_token_404(ui: TestClient) -> None:
    assert ui.get("/portal/not-a-real-token", follow_redirects=False).status_code == 404


def test_archived_project_link_404(ui: TestClient) -> None:
    pid = _make_project("Archived POC")
    ui.post(f"/ui/projects/{pid}/share-link/enable")
    token = _token_for(pid)
    assert ui.get(f"/portal/{token}").status_code == 200

    from app.db import get_session_factory
    from app.models import Project

    db = get_session_factory()()
    try:
        db.get(Project, pid).is_archived = True
        db.commit()
    finally:
        db.close()
    assert ui.get(f"/portal/{token}", follow_redirects=False).status_code == 404


def test_management_requires_login(client: TestClient) -> None:
    pid = _make_project("Guard POC")
    # No login → the internal-only dependency bounces to login; no link created.
    resp = client.post(f"/ui/projects/{pid}/share-link/enable", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/ui/login" in resp.headers.get("location", "")

    from app.db import get_session_factory
    from app.models import ProjectShareLink

    db = get_session_factory()()
    try:
        assert db.query(ProjectShareLink).filter_by(project_id=pid).first() is None
    finally:
        db.close()
