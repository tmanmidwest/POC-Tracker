"""Per-project use-case category ordering + collapsible Tasks section.

Covers the pure grouping/sort helper, the inline category-order endpoint
(set / change / clear), and the per-user tasks-collapsed preference.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    Customer,
    Project,
    ProjectCategoryOrder,
    ProjectStatus,
    ProjectUseCase,
    UseCaseStatus,
    UseCaseViewPref,
)
from app.ui.project_routes import _group_use_cases


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login", data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def admin_ui(client: TestClient) -> TestClient:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    return client


_seq = 0


def _project_with_categories(categories: list[str]) -> int:
    """Create a project with one use case in each given category."""
    global _seq
    _seq += 1
    db = get_session_factory()()
    try:
        cust = Customer(name=f"CatOrder Cust {_seq}")
        db.add(cust)
        db.flush()
        pstatus = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        ustatus = db.query(UseCaseStatus).order_by(UseCaseStatus.sort_order).first()
        project = Project(customer_id=cust.id, name="CatOrder POC", status_id=pstatus.id)
        db.add(project)
        db.flush()
        for i, cat in enumerate(categories):
            db.add(
                ProjectUseCase(
                    project_id=project.id, source="custom", category=cat,
                    name=f"UC {i}", reference_number="1.1", status_id=ustatus.id,
                )
            )
        db.commit()
        return project.id
    finally:
        db.close()


# --- pure grouping helper -------------------------------------------------


def _uc(category: str, ref: str | None = None, name: str = "x") -> ProjectUseCase:
    return ProjectUseCase(category=category, reference_number=ref, name=name)


def test_group_defaults_to_alphabetical() -> None:
    groups = _group_use_cases([_uc("JML"), _uc("Access Request"), _uc("Reporting")])
    assert [g["category"] for g in groups] == ["Access Request", "JML", "Reporting"]


def test_group_numbered_categories_sort_first() -> None:
    order = {"jml": 1, "access request": 2}
    groups = _group_use_cases(
        [_uc("JML"), _uc("Access Request"), _uc("Reporting")], order
    )
    # Numbered categories come first (by number), then un-numbered alphabetically.
    assert [g["category"] for g in groups] == ["JML", "Access Request", "Reporting"]
    assert groups[0]["order"] == 1
    assert groups[2]["order"] is None


def test_group_use_cases_within_category_sort_by_ref() -> None:
    groups = _group_use_cases(
        [_uc("A", "1.10"), _uc("A", "1.2"), _uc("A", "2.1")]
    )
    refs = [uc.reference_number for uc in groups[0]["use_cases"]]
    assert refs == ["1.2", "1.10", "2.1"]


# --- category-order endpoint ---------------------------------------------


def test_set_category_order_persists_and_reorders(admin_ui: TestClient) -> None:
    pid = _project_with_categories(["Access Request", "JML"])
    resp = admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        row = (
            db.query(ProjectCategoryOrder)
            .filter(ProjectCategoryOrder.project_id == pid)
            .one()
        )
        assert row.category == "JML" and row.sort_order == 1
    finally:
        db.close()
    # Detail page now lists JML before Access Request.
    html = admin_ui.get(f"/ui/projects/{pid}").text
    titles = re.findall(r'class="card__title">(JML|Access Request)</h3>', html)
    assert titles == ["JML", "Access Request"]


def test_set_category_order_upserts(admin_ui: TestClient) -> None:
    pid = _project_with_categories(["JML"])
    admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "5"}, follow_redirects=False,
    )
    admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "2"}, follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        rows = (
            db.query(ProjectCategoryOrder)
            .filter(ProjectCategoryOrder.project_id == pid)
            .all()
        )
        assert len(rows) == 1 and rows[0].sort_order == 2  # updated, not duplicated
    finally:
        db.close()


def test_clear_category_order_removes_row(admin_ui: TestClient) -> None:
    pid = _project_with_categories(["JML"])
    admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "3"}, follow_redirects=False,
    )
    admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": ""}, follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        count = (
            db.query(ProjectCategoryOrder)
            .filter(ProjectCategoryOrder.project_id == pid)
            .count()
        )
        assert count == 0
    finally:
        db.close()


def test_category_order_rejects_non_numeric(admin_ui: TestClient) -> None:
    pid = _project_with_categories(["JML"])
    resp = admin_ui.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "abc"}, follow_redirects=False,
    )
    assert resp.status_code == 303  # flashes error, no row written
    db = get_session_factory()()
    try:
        assert (
            db.query(ProjectCategoryOrder)
            .filter(ProjectCategoryOrder.project_id == pid)
            .count()
            == 0
        )
    finally:
        db.close()


def test_category_order_forbidden_for_external_viewer(client: TestClient) -> None:
    pid = _project_with_categories(["JML"])
    # Not logged in as internal — the internal-only guard rejects the request.
    resp = client.post(
        f"/ui/projects/{pid}/category-order",
        data={"category": "JML", "sort_order": "1"}, follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 401, 403)
    db = get_session_factory()()
    try:
        assert (
            db.query(ProjectCategoryOrder)
            .filter(ProjectCategoryOrder.project_id == pid)
            .count()
            == 0
        )
    finally:
        db.close()


# --- tasks collapsed preference ------------------------------------------


def _admin_user_id() -> int:
    from app.config import get_settings
    from app.models import AppUser

    s = get_settings()
    db = get_session_factory()()
    try:
        return (
            db.query(AppUser)
            .filter(AppUser.username == s.initial_admin_username)
            .one()
            .id
        )
    finally:
        db.close()


def test_tasks_collapsed_persists(admin_ui: TestClient) -> None:
    resp = admin_ui.post("/ui/projects/tasks-collapsed", data={"collapsed": "1"})
    assert resp.status_code == 204
    uid = _admin_user_id()
    db = get_session_factory()()
    try:
        pref = (
            db.query(UseCaseViewPref)
            .filter(UseCaseViewPref.app_user_id == uid)
            .one()
        )
        assert json.loads(pref.config_json)["tasks_collapsed"] is True
    finally:
        db.close()
    # Toggle back off.
    admin_ui.post("/ui/projects/tasks-collapsed", data={"collapsed": "0"})
    db = get_session_factory()()
    try:
        pref = (
            db.query(UseCaseViewPref)
            .filter(UseCaseViewPref.app_user_id == uid)
            .one()
        )
        assert json.loads(pref.config_json)["tasks_collapsed"] is False
    finally:
        db.close()


def test_saving_uc_view_preserves_tasks_collapsed(admin_ui: TestClient) -> None:
    admin_ui.post("/ui/projects/tasks-collapsed", data={"collapsed": "1"})
    # Saving the (separate) use-case field view must not wipe the collapse flag.
    pid = _project_with_categories(["JML"])
    admin_ui.post(
        f"/ui/projects/{pid}/use-case-view",
        data={"status_filter": "all", "field_ref": "1"}, follow_redirects=False,
    )
    uid = _admin_user_id()
    db = get_session_factory()()
    try:
        pref = (
            db.query(UseCaseViewPref)
            .filter(UseCaseViewPref.app_user_id == uid)
            .one()
        )
        assert json.loads(pref.config_json)["tasks_collapsed"] is True
    finally:
        db.close()
