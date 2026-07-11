"""New POC wizard: the bundled customer + project + use cases + tasks flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    Customer,
    LibrarySet,
    Project,
    ProjectUseCase,
    Task,
    UseCaseLibrary,
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


def _seed_library_entry(name: str = "SSO Login") -> int:
    db = get_session_factory()()
    try:
        lib_set = LibrarySet(name="Wizard Test Set", is_active=True, is_default=False)
        db.add(lib_set)
        db.flush()
        entry = UseCaseLibrary(
            library_set_id=lib_set.id,
            category="Security",
            name=name,
            description="Validate single sign-on.",
            is_active=True,
        )
        db.add(entry)
        db.commit()
        return entry.id
    finally:
        db.close()


def _project_by_customer(customer_name: str) -> Project | None:
    db = get_session_factory()()
    try:
        cust = db.query(Customer).filter(Customer.name == customer_name).one_or_none()
        if cust is None:
            return None
        return (
            db.query(Project)
            .filter(Project.customer_id == cust.id)
            .one_or_none()
        )
    finally:
        db.close()


def test_wizard_page_renders(admin_ui: TestClient) -> None:
    resp = admin_ui.get("/ui/projects/wizard")
    assert resp.status_code == 200
    assert "New POC" in resp.text
    # The nav link should be present too.
    assert "/ui/projects/wizard" in resp.text


def test_wizard_creates_full_poc_atomically(admin_ui: TestClient) -> None:
    lib_id = _seed_library_entry()
    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={
            "customer_mode": "new",
            "new_customer_name": "Acme Corp",
            "new_customer_website": "acme.com",
            "name": "Acme POC",
            "library_ids": [str(lib_id)],
            "custom_category": ["Reporting"],
            "custom_name": ["Custom dashboard"],
            "custom_description": [""],
            "custom_success": [""],
            "task_title": ["Schedule kickoff"],
            "task_start": [""],
            "task_due": ["2026-08-01"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    project = _project_by_customer("Acme Corp")
    assert project is not None
    assert resp.headers["location"] == f"/ui/projects/{project.id}"

    db = get_session_factory()()
    try:
        # New customer created with normalized website.
        cust = db.query(Customer).filter(Customer.name == "Acme Corp").one()
        assert cust.website == "acme.com"
        # One library + one custom use case.
        ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == project.id).all()
        assert len(ucs) == 2
        assert {uc.name for uc in ucs} == {"SSO Login", "Custom dashboard"}
        # One task attached to the project.
        tasks = db.query(Task).filter(Task.project_id == project.id).all()
        assert len(tasks) == 1
        assert tasks[0].title == "Schedule kickoff"
    finally:
        db.close()


def test_wizard_existing_customer(admin_ui: TestClient) -> None:
    db = get_session_factory()()
    try:
        cust = Customer(name="Existing Inc")
        db.add(cust)
        db.commit()
        cust_id = cust.id
    finally:
        db.close()

    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={
            "customer_mode": "existing",
            "existing_customer_id": str(cust_id),
            "name": "Existing POC",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    db = get_session_factory()()
    try:
        # No duplicate customer was created.
        assert db.query(Customer).filter(Customer.name == "Existing Inc").count() == 1
        proj = db.query(Project).filter(Project.customer_id == cust_id).one()
        assert proj.name == "Existing POC"
    finally:
        db.close()


def test_wizard_duplicate_customer_name_is_rejected_without_orphan(admin_ui: TestClient) -> None:
    db = get_session_factory()()
    try:
        db.add(Customer(name="Dupe Co"))
        db.commit()
    finally:
        db.close()

    before = _count(Project)
    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={"customer_mode": "new", "new_customer_name": "Dupe Co", "name": "Should not exist"},
        follow_redirects=False,
    )
    # Re-renders the wizard (200) with an error, and creates nothing.
    assert resp.status_code == 200
    assert "already exists" in resp.text
    assert _count(Project) == before
    assert _count(Customer, name="Dupe Co") == 1


def test_wizard_requires_a_customer(admin_ui: TestClient) -> None:
    before = _count(Project)
    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={"customer_mode": "new", "new_customer_name": "", "name": "Orphan"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "customer is required" in resp.text.lower()
    assert _count(Project) == before


def _count(model, **filters) -> int:
    db = get_session_factory()()
    try:
        q = db.query(model)
        for k, v in filters.items():
            q = q.filter(getattr(model, k) == v)
        return q.count()
    finally:
        db.close()
