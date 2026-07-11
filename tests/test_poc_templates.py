"""POC templates: save-as-template, the management pages, and wizard pre-fill."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    AppUser,
    Customer,
    PocTemplate,
    Project,
    ProjectStatus,
    ProjectUseCase,
    Task,
    TaskStatus,
    UseCaseStatus,
)


def _login(client: TestClient) -> None:
    from app.config import get_settings

    s = get_settings()
    resp = client.post(
        "/ui/login",
        data={"username": s.initial_admin_username, "password": s.initial_admin_password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def admin_ui(client: TestClient) -> TestClient:
    _login(client)
    return client


def _seed_project_with_content() -> int:
    db = get_session_factory()()
    try:
        cust = Customer(name="TplCo")
        db.add(cust)
        db.flush()
        pstatus = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        proj = Project(
            customer_id=cust.id, name="Src POC", status_id=pstatus.id,
            start_date=date(2026, 8, 1),
        )
        db.add(proj)
        db.flush()
        ucstatus = db.query(UseCaseStatus).order_by(UseCaseStatus.sort_order).first()
        db.add(ProjectUseCase(
            project_id=proj.id, source="custom", category="Security",
            name="SSO Login", description="Validate SSO.", status_id=ucstatus.id,
        ))
        tstatus = (
            db.query(TaskStatus)
            .filter(TaskStatus.is_terminal.is_(False))
            .order_by(TaskStatus.sort_order)
            .first()
        )
        admin = db.query(AppUser).order_by(AppUser.id).first()
        db.add(Task(
            owner_user_id=admin.id, title="Kickoff call", status_id=tstatus.id,
            project_id=proj.id, start_date=date(2026, 8, 1), due_date=date(2026, 8, 4),
        ))
        db.commit()
        return proj.id
    finally:
        db.close()


def test_save_project_as_template_snapshots_content(admin_ui: TestClient) -> None:
    project_id = _seed_project_with_content()
    resp = admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Standard ISPM", "description": "Our go-to POC."},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    db = get_session_factory()()
    try:
        tpl = db.query(PocTemplate).filter(PocTemplate.name == "Standard ISPM").one()
        assert resp.headers["location"] == f"/ui/templates/{tpl.id}"
        assert len(tpl.use_cases) == 1
        assert tpl.use_cases[0].name == "SSO Login"
        assert len(tpl.tasks) == 1
        # Task due 2026-08-04 vs project start 2026-08-01 → offset of 3 days.
        assert tpl.tasks[0].due_offset_days == 3
        assert tpl.tasks[0].start_offset_days == 0
    finally:
        db.close()


def test_duplicate_template_name_rejected(admin_ui: TestClient) -> None:
    project_id = _seed_project_with_content()
    admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Dupe Template"}, follow_redirects=False,
    )
    admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Dupe Template"}, follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert db.query(PocTemplate).filter(PocTemplate.name == "Dupe Template").count() == 1
    finally:
        db.close()


def test_templates_list_and_detail_render(admin_ui: TestClient) -> None:
    project_id = _seed_project_with_content()
    admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Viewable Tpl"}, follow_redirects=False,
    )
    listing = admin_ui.get("/ui/templates")
    assert listing.status_code == 200
    assert "Viewable Tpl" in listing.text

    db = get_session_factory()()
    try:
        tid = db.query(PocTemplate).filter(PocTemplate.name == "Viewable Tpl").one().id
    finally:
        db.close()
    detail = admin_ui.get(f"/ui/templates/{tid}")
    assert detail.status_code == 200
    assert "SSO Login" in detail.text
    assert "Kickoff call" in detail.text


def test_wizard_prefills_from_template(admin_ui: TestClient) -> None:
    project_id = _seed_project_with_content()
    admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Prefill Tpl"}, follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        tid = db.query(PocTemplate).filter(PocTemplate.name == "Prefill Tpl").one().id
    finally:
        db.close()

    resp = admin_ui.get(f"/ui/projects/wizard?template_id={tid}")
    assert resp.status_code == 200
    # Custom use case snapshot pre-filled into a row, and the task title too.
    assert "SSO Login" in resp.text
    assert "Kickoff call" in resp.text
    assert "Pre-filled from" in resp.text


def test_delete_template(admin_ui: TestClient) -> None:
    project_id = _seed_project_with_content()
    admin_ui.post(
        f"/ui/projects/{project_id}/save-as-template",
        data={"name": "Deleteme Tpl"}, follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        tid = db.query(PocTemplate).filter(PocTemplate.name == "Deleteme Tpl").one().id
    finally:
        db.close()

    resp = admin_ui.post(f"/ui/templates/{tid}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        assert db.get(PocTemplate, tid) is None
    finally:
        db.close()
