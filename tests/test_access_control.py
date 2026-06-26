"""Per-project access control: external-viewer scoping, grant authority, JIT tier."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser, Customer, Project, ProjectGrant, ProjectStatus
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
        assert can_grant_project(db.get(AppUser, se_id), project) is True
        assert can_grant_project(db.get(AppUser, admin_id), project) is True
        assert can_grant_project(db.get(AppUser, other_id), project) is False
        assert can_grant_project(db.get(AppUser, ext_id), project) is False
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
