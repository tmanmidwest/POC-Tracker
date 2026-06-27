"""Bulk use-case actions: status, feature type, completed-on, delete."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    AppUser,
    Customer,
    FeatureType,
    Project,
    ProjectStatus,
    ProjectUseCase,
    UseCaseStatus,
)
from app.services.passwords import hash_password


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


def _project_with_use_cases(n: int) -> tuple[int, list[int]]:
    global _seq
    _seq += 1
    db = get_session_factory()()
    try:
        cust = Customer(name=f"Bulk Cust {_seq}")
        db.add(cust)
        db.flush()
        pstatus = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        ustatus = db.query(UseCaseStatus).order_by(UseCaseStatus.sort_order).first()
        project = Project(customer_id=cust.id, name="Bulk POC", status_id=pstatus.id)
        db.add(project)
        db.flush()
        ids = []
        for i in range(n):
            uc = ProjectUseCase(
                project_id=project.id, source="custom", category="Cat",
                name=f"UC {i}", status_id=ustatus.id,
            )
            db.add(uc)
            db.flush()
            ids.append(uc.id)
        db.commit()
        return project.id, ids
    finally:
        db.close()


def _complete_status_id() -> int:
    db = get_session_factory()()
    try:
        st = db.query(UseCaseStatus).filter(UseCaseStatus.is_complete_status.is_(True)).first()
        return st.id
    finally:
        db.close()


def test_bulk_set_status_with_stamp(admin_ui: TestClient) -> None:
    pid, ids = _project_with_use_cases(3)
    cid = _complete_status_id()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "status", "status_id": str(cid), "stamp_today": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
        assert all(uc.status_id == cid for uc in ucs)
        assert all(uc.completed_on == date.today() for uc in ucs)  # stamped
    finally:
        db.close()


def test_bulk_set_category(admin_ui: TestClient) -> None:
    pid, ids = _project_with_use_cases(2)
    resp = admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "category", "category": "Lifecycle Management"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
        assert all(uc.category == "Lifecycle Management" for uc in ucs)
    finally:
        db.close()


def test_bulk_set_feature_type(admin_ui: TestClient) -> None:
    pid, ids = _project_with_use_cases(2)
    db = get_session_factory()()
    try:
        ft = db.query(FeatureType).first()
        ft_id = ft.id
    finally:
        db.close()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "feature_type", "feature_type_id": str(ft_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
        assert all(uc.feature_type_id == ft_id for uc in ucs)
    finally:
        db.close()


def test_bulk_completed_on_set_and_clear(admin_ui: TestClient) -> None:
    pid, ids = _project_with_use_cases(2)
    admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "completed_on", "completed_on": "2026-05-01"},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert all(
            uc.completed_on == date(2026, 5, 1)
            for uc in db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid)
        )
    finally:
        db.close()
    # Clearing: empty date wipes it.
    admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "completed_on", "completed_on": ""},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert all(
            uc.completed_on is None
            for uc in db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid)
        )
    finally:
        db.close()


def test_bulk_delete(admin_ui: TestClient) -> None:
    pid, ids = _project_with_use_cases(3)
    resp = admin_ui.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids[:2], "action": "delete"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        remaining = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
        assert len(remaining) == 1  # one left
    finally:
        db.close()


def test_bulk_scoped_to_project(admin_ui: TestClient) -> None:
    """Ids belonging to another project are ignored."""
    pid_a, ids_a = _project_with_use_cases(1)
    _pid_b, ids_b = _project_with_use_cases(1)
    cid = _complete_status_id()
    # Target project A but include project B's id — B must be untouched.
    admin_ui.post(
        f"/ui/projects/{pid_a}/use-cases/bulk",
        data={"ids": ids_a + ids_b, "action": "status", "status_id": str(cid)},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert db.get(ProjectUseCase, ids_a[0]).status_id == cid
        assert db.get(ProjectUseCase, ids_b[0]).status_id != cid  # other project untouched
    finally:
        db.close()


def test_bulk_forbidden_for_external_viewer(client: TestClient) -> None:
    pid, ids = _project_with_use_cases(1)
    db = get_session_factory()()
    try:
        db.add(
            AppUser(
                username="bulkviewer", password_hash=hash_password("password123"),
                is_active=True, is_external=True,
            )
        )
        db.commit()
    finally:
        db.close()
    _login(client, "bulkviewer", "password123")
    resp = client.post(
        f"/ui/projects/{pid}/use-cases/bulk",
        data={"ids": ids, "action": "delete"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/ui/dashboard" in resp.headers.get("location", "")  # forbidden → dashboard
    db = get_session_factory()()
    try:
        assert db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).count() == 1
    finally:
        db.close()
