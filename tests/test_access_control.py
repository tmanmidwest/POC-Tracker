"""Per-project access control: external-viewer scoping, grant authority, JIT tier."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    AppUser,
    Customer,
    Project,
    ProjectGrant,
    ProjectNote,
    ProjectStatus,
    Task,
    TaskStatus,
)
from app.services.passwords import hash_password

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def admin_ui(client: TestClient) -> TestClient:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    return client


def _make_user(
    username: str, *, is_admin: bool = False, is_external: bool = False
) -> int:
    db = get_session_factory()()
    try:
        u = AppUser(
            username=username,
            display_name=username.title(),
            password_hash=hash_password("password123"),
            is_active=True,
            is_admin=is_admin,
            is_external=is_external,
        )
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _make_project(name: str, *, sales_engineer_id: int | None = None) -> int:
    db = get_session_factory()()
    try:
        customer = Customer(name=f"Cust {name}")
        db.add(customer)
        db.flush()
        status = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        project = Project(
            customer_id=customer.id,
            name=name,
            status_id=status.id,
            sales_engineer_id=sales_engineer_id,
        )
        db.add(project)
        db.commit()
        return project.id
    finally:
        db.close()


def _grant(project_id: int, user_id: int) -> None:
    db = get_session_factory()()
    try:
        db.add(ProjectGrant(project_id=project_id, user_id=user_id))
        db.commit()
    finally:
        db.close()


def _add_task(
    project_id: int, *, owner_id: int, title: str, is_internal_only: bool = False
) -> int:
    db = get_session_factory()()
    try:
        status_id = db.query(TaskStatus).order_by(TaskStatus.sort_order).first().id
        task = Task(
            owner_user_id=owner_id,
            title=title,
            status_id=status_id,
            project_id=project_id,
            is_internal_only=is_internal_only,
        )
        db.add(task)
        db.commit()
        return task.id
    finally:
        db.close()


def _add_note(project_id: int, *, body: str, is_internal_only: bool = False) -> int:
    db = get_session_factory()()
    try:
        note = ProjectNote(
            project_id=project_id,
            note_date=date.today(),
            body=body,
            body_html=None,
            created_by="tester",
            is_internal_only=is_internal_only,
        )
        db.add(note)
        db.commit()
        return note.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Access service (unit)
# ---------------------------------------------------------------------------


def test_accessible_ids_none_for_internal(client: TestClient) -> None:
    """Internal users get None (= all projects, no filter)."""
    from app.services.access import accessible_project_ids

    _make_project("P1")
    std_id = _make_user("standard1")
    db = get_session_factory()()
    try:
        std = db.get(AppUser, std_id)
        assert accessible_project_ids(db, std) is None
    finally:
        db.close()


def test_accessible_ids_scoped_for_external(client: TestClient) -> None:
    """External viewers only get the ids granted to them."""
    from app.services.access import accessible_project_ids

    p1 = _make_project("P1")
    _make_project("P2")  # not granted
    ext_id = _make_user("ext1", is_external=True)
    _grant(p1, ext_id)
    db = get_session_factory()()
    try:
        ext = db.get(AppUser, ext_id)
        assert accessible_project_ids(db, ext) == {p1}
    finally:
        db.close()


def test_can_grant_authority(client: TestClient) -> None:
    from app.services.access import can_grant_project

    se_id = _make_user("se1")
    other_id = _make_user("other1")
    admin_id = _make_user("admin1", is_admin=True)
    ext_id = _make_user("ext2", is_external=True)
    pid = _make_project("Owned", sales_engineer_id=se_id)
    db = get_session_factory()()
    try:
        project = db.get(Project, pid)
        assert can_grant_project(db, db.get(AppUser, se_id), project) is True
        assert can_grant_project(db, db.get(AppUser, admin_id), project) is True
        assert can_grant_project(db, db.get(AppUser, other_id), project) is False
        assert can_grant_project(db, db.get(AppUser, ext_id), project) is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# External viewer scoping (UI, end-to-end)
# ---------------------------------------------------------------------------


def test_external_viewer_sees_only_granted(client: TestClient) -> None:
    granted = _make_project("Granted POC")
    hidden = _make_project("Hidden POC")
    ext_id = _make_user("viewer", is_external=True)
    _grant(granted, ext_id)

    _login(client, "viewer", "password123")

    listing = client.get("/ui/projects")
    assert listing.status_code == 200
    assert "Granted POC" in listing.text
    assert "Hidden POC" not in listing.text

    # Granted project is viewable; ungranted one 404s (can't even probe it).
    assert client.get(f"/ui/projects/{granted}").status_code == 200
    assert client.get(f"/ui/projects/{hidden}").status_code == 404


def test_external_viewer_is_read_only(client: TestClient) -> None:
    pid = _make_project("RO POC")
    ext_id = _make_user("viewer2", is_external=True)
    _grant(pid, ext_id)

    _login(client, "viewer2", "password123")

    # A mutating route is blocked (forbidden → redirected to dashboard).
    resp = client.post(
        f"/ui/projects/{pid}/use-cases",
        data={"category": "X", "name": "Y"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/ui/dashboard" in resp.headers.get("location", "")

    # Internal-only sections are forbidden to external viewers (→ dashboard).
    cust = client.get("/ui/customers/", follow_redirects=False)
    assert cust.status_code == 303
    assert "/ui/dashboard" in cust.headers.get("location", "")

    # The read-only detail page shows no edit affordance.
    page = client.get(f"/ui/projects/{pid}").text
    assert f"/ui/projects/{pid}/edit" not in page


# ---------------------------------------------------------------------------
# Grant routes (authority enforced server-side)
# ---------------------------------------------------------------------------


def test_se_can_grant_non_se_cannot(client: TestClient) -> None:
    se_id = _make_user("se_owner")
    pid = _make_project("SE Project", sales_engineer_id=se_id)
    ext_id = _make_user("guest", is_external=True)

    # The SE sees the Share panel on their project's detail page.
    _login(client, "se_owner", "password123")
    assert "Shared access" in client.get(f"/ui/projects/{pid}").text

    # The SE can share their project.
    resp = client.post(
        f"/ui/projects/{pid}/grants",
        data={"user_id": ext_id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = get_session_factory()()
    try:
        assert (
            db.query(ProjectGrant)
            .filter(ProjectGrant.project_id == pid, ProjectGrant.user_id == ext_id)
            .count()
            == 1
        )
    finally:
        db.close()

    # A standard user who is not the SE cannot grant on it.
    _make_user("bystander")
    other = TestClient(client.app)
    _login(other, "bystander", "password123")
    resp2 = other.post(
        f"/ui/projects/{pid}/grants",
        data={"user_id": ext_id},
        follow_redirects=False,
    )
    assert resp2.status_code == 403


# ---------------------------------------------------------------------------
# JIT provisioning honors the provider's default tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "expect_external"),
    [("external", True), ("standard", False)],
)
def test_jit_tier_from_provider(client: TestClient, tier: str, expect_external: bool) -> None:
    from app.models import AuthProvider
    from app.services.oidc import find_or_create_user

    db = get_session_factory()()
    try:
        provider = AuthProvider(
            slug=f"prov-{tier}",
            display_name="Test IdP",
            issuer_url="https://idp.example.com",
            client_id="cid",
            default_user_tier=tier,
        )
        db.add(provider)
        db.commit()

        user = find_or_create_user(
            db, provider, {"sub": f"sub-{tier}", "email": f"{tier}@example.com"}
        )
        assert user.is_external is expect_external
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Internal-only notes: hidden from external viewers everywhere
# ---------------------------------------------------------------------------


def test_visible_project_notes_filters_for_external(client: TestClient) -> None:
    """The helper returns every note to internal users, and drops internal-only
    notes for external viewers."""
    from app.services.access import visible_project_notes

    pid = _make_project("Notes POC")
    _add_note(pid, body="Shared update")
    _add_note(pid, body="Internal secret", is_internal_only=True)
    std_id = _make_user("std_notes")
    ext_id = _make_user("ext_notes", is_external=True)

    db = get_session_factory()()
    try:
        project = db.get(Project, pid)
        internal_bodies = {n.body for n in visible_project_notes(project, db.get(AppUser, std_id))}
        external_bodies = {n.body for n in visible_project_notes(project, db.get(AppUser, ext_id))}
    finally:
        db.close()

    assert internal_bodies == {"Shared update", "Internal secret"}
    assert external_bodies == {"Shared update"}


def test_external_detail_page_hides_internal_only_note(client: TestClient) -> None:
    """An external viewer's project page shows shared notes but not internal-only ones."""
    pid = _make_project("Detail POC")
    _add_note(pid, body="VISIBLE_SHARED_NOTE")
    _add_note(pid, body="HIDDEN_INTERNAL_NOTE", is_internal_only=True)
    ext_id = _make_user("ext_detail", is_external=True)
    _grant(pid, ext_id)

    _login(client, "ext_detail", "password123")
    page = client.get(f"/ui/projects/{pid}")
    assert page.status_code == 200
    assert "VISIBLE_SHARED_NOTE" in page.text
    assert "HIDDEN_INTERNAL_NOTE" not in page.text


def test_external_report_and_pdf_exclude_internal_only_note(client: TestClient) -> None:
    """The report page (shared by the PDF) never renders an internal-only note
    for an external viewer."""
    pid = _make_project("Report POC")
    _add_note(pid, body="VISIBLE_SHARED_NOTE")
    _add_note(pid, body="HIDDEN_INTERNAL_NOTE", is_internal_only=True)
    ext_id = _make_user("ext_report", is_external=True)
    _grant(pid, ext_id)

    _login(client, "ext_report", "password123")
    report = client.get(f"/ui/reports/projects/{pid}")
    assert report.status_code == 200
    assert "VISIBLE_SHARED_NOTE" in report.text
    assert "HIDDEN_INTERNAL_NOTE" not in report.text


def test_internal_user_sees_internal_only_note_and_badge(client: TestClient) -> None:
    """Internal users see internal-only notes, flagged with the badge."""
    pid = _make_project("Internal View POC")
    _add_note(pid, body="HIDDEN_INTERNAL_NOTE", is_internal_only=True)

    _make_user("std_view")
    _login(client, "std_view", "password123")
    page = client.get(f"/ui/projects/{pid}")
    assert page.status_code == 200
    assert "HIDDEN_INTERNAL_NOTE" in page.text
    assert "Internal only" in page.text  # the badge


# ---------------------------------------------------------------------------
# External viewers see a project's non-internal-only tasks (read-only) — Phase 4
# ---------------------------------------------------------------------------


def test_external_viewer_sees_project_tasks_except_internal_only(client: TestClient) -> None:
    pid = _make_project("Task Vis POC")
    owner = _make_user("task_owner")  # an internal owner of the tasks
    _add_task(pid, owner_id=owner, title="SHARED_TASK")
    _add_task(pid, owner_id=owner, title="SECRET_TASK", is_internal_only=True)
    ext = _make_user("task_viewer", is_external=True)
    _grant(pid, ext)

    _login(client, "task_viewer", "password123")
    page = client.get(f"/ui/projects/{pid}")
    assert page.status_code == 200
    assert "SHARED_TASK" in page.text
    assert "SECRET_TASK" not in page.text


def test_external_task_view_is_read_only(client: TestClient) -> None:
    pid = _make_project("RO Tasks POC")
    owner = _make_user("ro_owner")
    tid = _add_task(pid, owner_id=owner, title="RO_TASK")
    ext = _make_user("ro_viewer", is_external=True)
    _grant(pid, ext)

    _login(client, "ro_viewer", "password123")
    page = client.get(f"/ui/projects/{pid}").text
    assert "RO_TASK" in page
    # No mutating affordances for an external viewer.
    assert f"/ui/tasks/new?project_id={pid}" not in page
    assert f"/ui/tasks/{tid}/edit" not in page
    assert f"/ui/tasks/{tid}/status" not in page


def test_admin_sees_all_project_tasks_including_internal_only(admin_ui: TestClient) -> None:
    pid = _make_project("Admin Tasks POC")
    owner = _make_user("someone_else")
    _add_task(pid, owner_id=owner, title="A_SHARED_TASK")
    _add_task(pid, owner_id=owner, title="A_SECRET_TASK", is_internal_only=True)

    page = admin_ui.get(f"/ui/projects/{pid}").text
    # Admin sees everyone's tasks, including internal-only, with edit controls.
    assert "A_SHARED_TASK" in page and "A_SECRET_TASK" in page
    assert f"/ui/tasks/new?project_id={pid}" in page


# ---------------------------------------------------------------------------
# Report audience: client-facing vs internal (internal-only items in reports)
# ---------------------------------------------------------------------------


def test_report_default_is_client_facing_for_internal_user(client: TestClient) -> None:
    """With no audience chosen, even an internal user gets the client-facing
    report: internal-only notes and tasks are excluded by default."""
    pid = _make_project("Audience Default POC")
    owner = _make_user("aud_owner")
    _add_note(pid, body="SHARED_NOTE")
    _add_note(pid, body="INTERNAL_NOTE", is_internal_only=True)
    _add_task(pid, owner_id=owner, title="SHARED_TASK")
    _add_task(pid, owner_id=owner, title="INTERNAL_TASK", is_internal_only=True)

    _make_user("aud_std")
    _login(client, "aud_std", "password123")
    page = client.get(f"/ui/reports/projects/{pid}")
    assert page.status_code == 200
    assert "SHARED_NOTE" in page.text and "SHARED_TASK" in page.text
    assert "INTERNAL_NOTE" not in page.text
    assert "INTERNAL_TASK" not in page.text


def test_report_internal_audience_includes_internal_items(client: TestClient) -> None:
    """An internal user can request the internal audience and get everything,
    with internal-only items flagged."""
    pid = _make_project("Audience Internal POC")
    owner = _make_user("aud_owner2")
    _add_note(pid, body="SHARED_NOTE")
    _add_note(pid, body="INTERNAL_NOTE", is_internal_only=True)
    _add_task(pid, owner_id=owner, title="SHARED_TASK")
    _add_task(pid, owner_id=owner, title="INTERNAL_TASK", is_internal_only=True)

    _make_user("aud_std2")
    _login(client, "aud_std2", "password123")
    page = client.get(f"/ui/reports/projects/{pid}?audience=internal")
    assert page.status_code == 200
    assert "INTERNAL_NOTE" in page.text and "INTERNAL_TASK" in page.text
    assert "Internal only" in page.text  # the item badge


def test_report_external_viewer_cannot_force_internal_audience(
    client: TestClient,
) -> None:
    """An external viewer requesting audience=internal is still denied internal
    items — the audience can only ever reduce what an external viewer sees."""
    pid = _make_project("Audience Ext POC")
    owner = _make_user("aud_owner3")
    _add_note(pid, body="SHARED_NOTE")
    _add_note(pid, body="INTERNAL_NOTE", is_internal_only=True)
    _add_task(pid, owner_id=owner, title="SHARED_TASK")
    _add_task(pid, owner_id=owner, title="INTERNAL_TASK", is_internal_only=True)
    ext = _make_user("aud_ext", is_external=True)
    _grant(pid, ext)

    _login(client, "aud_ext", "password123")
    page = client.get(f"/ui/reports/projects/{pid}?audience=internal")
    assert page.status_code == 200
    assert "SHARED_NOTE" in page.text and "SHARED_TASK" in page.text
    assert "INTERNAL_NOTE" not in page.text
    assert "INTERNAL_TASK" not in page.text


def test_report_includes_all_owners_tasks(client: TestClient) -> None:
    """A report covers the whole POC: a standard user's report includes tasks
    owned by others (unlike the per-user Task Manager, which scopes to own)."""
    pid = _make_project("Report Tasks POC")
    other = _make_user("other_owner")
    _add_task(pid, owner_id=other, title="OTHERS_TASK")

    _make_user("report_std")
    _login(client, "report_std", "password123")
    page = client.get(f"/ui/reports/projects/{pid}")
    assert page.status_code == 200
    assert "OTHERS_TASK" in page.text


# ---------------------------------------------------------------------------
# Nav: API Docs link is internal-only
# ---------------------------------------------------------------------------


def test_api_docs_link_hidden_from_external_users(client: TestClient) -> None:
    _make_user("nav_ext", is_external=True)
    _login(client, "nav_ext", "password123")
    page = client.get("/ui/dashboard")
    assert page.status_code == 200
    assert "API Docs" not in page.text


def test_api_docs_link_shown_to_internal_users(client: TestClient) -> None:
    _make_user("nav_std")
    _login(client, "nav_std", "password123")
    page = client.get("/ui/dashboard")
    assert page.status_code == 200
    assert "API Docs" in page.text
