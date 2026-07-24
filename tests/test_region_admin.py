"""Region RBAC admin surfaces: role model, memberships, lookup, bulk assignment.

Covers Phase 1 of the region-based access rollout (data model + admin UI). Does
NOT cover enforcement (a standard user still sees all projects until Phase 2/3);
those assertions live with the enforcement work.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AppUser, Customer, Project, ProjectStatus, Region, UserRegion
from app.services.passwords import hash_password
from app.services.regions import (
    backfill_project_regions,
    bulk_set_regions,
    get_user_region_ids,
    parse_region_csv,
    resolve_se_region_id,
    set_user_regions,
)


def _login_admin(client: TestClient) -> None:
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
    _login_admin(client)
    return client


def _mk_user(username: str, role: str, email: str | None = None) -> int:
    db = get_session_factory()()
    try:
        u = AppUser(
            username=username,
            email=email,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        u.role = role
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _mk_region(name: str, sort_order: int = 100) -> int:
    db = get_session_factory()()
    try:
        r = Region(name=name, sort_order=sort_order, is_active=True, is_system=False)
        db.add(r)
        db.commit()
        return r.id
    finally:
        db.close()


def _mk_project(name: str, sales_engineer_id: int | None = None) -> int:
    db = get_session_factory()()
    try:
        cust = Customer(name=f"Cust {name}")
        db.add(cust)
        db.flush()
        status = db.query(ProjectStatus).first()
        p = Project(
            customer_id=cust.id,
            name=name,
            status_id=status.id,
            sales_engineer_id=sales_engineer_id,
        )
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


# --- role model -------------------------------------------------------------


@pytest.mark.parametrize(
    "role,is_admin,is_external,is_manager,internal",
    [
        ("admin", True, False, False, True),
        ("manager", False, False, True, True),
        ("standard", False, False, False, True),
        ("external", False, True, False, False),
    ],
)
def test_role_setter_maps_to_flags(role, is_admin, is_external, is_manager, internal):
    u = AppUser(username="x")
    u.role = role
    assert u.role == role
    assert u.is_admin is is_admin
    assert u.is_external is is_external
    assert u.is_manager is is_manager
    assert u.is_internal is internal


def test_role_setter_rejects_unknown():
    with pytest.raises(ValueError):
        AppUser(username="x").role = "wizard"


# --- membership service -----------------------------------------------------


def test_set_user_regions_reconciles(client: TestClient):
    with client:  # trigger lifespan (migrations + seed)
        uid = _mk_user("se1", "standard")
        a, e, p = _mk_region("AMER", 10), _mk_region("EMEA", 20), _mk_region("APAC", 30)
        db = get_session_factory()()
        try:
            set_user_regions(db, uid, [a, e])
            db.commit()
            assert get_user_region_ids(db, uid) == {a, e}
            # reconcile: drop EMEA, add APAC
            set_user_regions(db, uid, [a, p])
            db.commit()
            assert get_user_region_ids(db, uid) == {a, p}
            # unknown region id silently ignored
            set_user_regions(db, uid, [a, 99999])
            db.commit()
            assert get_user_region_ids(db, uid) == {a}
        finally:
            db.close()


def test_parse_region_csv_variants():
    text = (
        "identifier,regions\n"
        "jane@x.com,AMER\n"
        "carlos@x.com,EMEA;APAC\n"
        "se_bob,AMER|EMEA\n"
        "\n"
        "  ,skipme\n"
        "nolregions,\n"
    )
    assert parse_region_csv(text) == [
        ("jane@x.com", ["AMER"]),
        ("carlos@x.com", ["EMEA", "APAC"]),
        ("se_bob", ["AMER", "EMEA"]),
        ("nolregions", []),
    ]


def test_bulk_set_regions_summary(client: TestClient):
    with client:
        se = _mk_user("se1", "standard", "jane@x.com")
        se2 = _mk_user("se2", "standard")
        mgr = _mk_user("mgr1", "manager", "carlos@x.com")
        ext = _mk_user("ext1", "external", "e@x.com")
        a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
        db = get_session_factory()()
        try:
            summary = bulk_set_regions(
                db,
                [
                    ("jane@x.com", ["AMER"]),  # match by email
                    ("mgr1", ["AMER", "EMEA"]),  # match by username, multi
                    ("e@x.com", ["AMER"]),  # external -> skipped
                    ("ghost@x.com", ["AMER"]),  # no match
                    ("se2", ["Atlantis"]),  # unknown region -> se2 ends empty
                ],
            )
            db.commit()
            assert get_user_region_ids(db, se) == {a}
            assert get_user_region_ids(db, mgr) == {a, e}
            assert get_user_region_ids(db, se2) == set()
            assert get_user_region_ids(db, ext) == set()
            assert "ghost@x.com" in summary["unmatched"]
            assert "e@x.com" in summary["skipped"]
            assert "Atlantis" in summary["unknown_regions"]
            assert "se2" in summary["updated"]  # matched, even if net-empty
        finally:
            db.close()


# --- lookup delete guard ----------------------------------------------------


def test_region_delete_blocked_when_in_use(admin_ui: TestClient):
    rid = _mk_region("AMER", 10)
    uid = _mk_user("se1", "standard")
    db = get_session_factory()()
    try:
        db.add(UserRegion(user_id=uid, region_id=rid))
        db.commit()
    finally:
        db.close()
    # UI delete should be blocked (membership present)
    admin_ui.post(f"/ui/lookups/regions/{rid}/delete", follow_redirects=False)
    db = get_session_factory()()
    try:
        assert db.get(Region, rid) is not None  # survived
    finally:
        db.close()


def test_system_unassigned_region_seeded_and_protected(admin_ui: TestClient):
    db = get_session_factory()()
    try:
        sys_region = db.query(Region).filter_by(is_system=True).one()
        assert sys_region.name == "Unassigned"
        rid = sys_region.id
    finally:
        db.close()
    admin_ui.post(f"/ui/lookups/regions/{rid}/delete", follow_redirects=False)
    db = get_session_factory()()
    try:
        assert db.get(Region, rid) is not None  # system row not deletable
    finally:
        db.close()


# --- bulk assignment routes -------------------------------------------------


def test_bulk_regions_grid_and_csv(admin_ui: TestClient):
    se = _mk_user("se1", "standard", "jane@x.com")
    mgr = _mk_user("mgr1", "manager")
    a, e, p = _mk_region("AMER", 10), _mk_region("EMEA", 20), _mk_region("APAC", 30)

    # grid excludes admin, lists scoped users
    page = admin_ui.get("/ui/settings/bulk-regions").text
    assert "se1" in page and "mgr1" in page

    # grid save (repeated keys as a browser posts checkboxes)
    admin_ui.post(
        "/ui/settings/bulk-regions",
        data={"user_ids": [str(se), str(mgr)], f"regions_{se}": [str(a)],
              f"regions_{mgr}": [str(a), str(p)]},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert get_user_region_ids(db, se) == {a}
        assert get_user_region_ids(db, mgr) == {a, p}
    finally:
        db.close()

    # CSV import reassigns
    csv = b"identifier,regions\njane@x.com,EMEA\nmgr1,AMER;EMEA\n"
    admin_ui.post(
        "/ui/settings/bulk-regions/import",
        files={"csv_file": ("a.csv", io.BytesIO(csv), "text/csv")},
        follow_redirects=False,
    )
    db = get_session_factory()()
    try:
        assert get_user_region_ids(db, se) == {e}
        assert get_user_region_ids(db, mgr) == {a, e}
    finally:
        db.close()


# --- Phase 4: enforcement flag + backfill ----------------------------------


def test_enforcement_flag_defaults_off_and_toggles(admin_ui: TestClient):
    from app.services import system_config

    assert system_config.region_enforcement_enabled() is False
    # Enable via the System settings form.
    admin_ui.post(
        "/ui/settings/system",
        data={"audit_retention_days": "30", "external_user_ttl_days": "60",
              "tasks_enabled": "1", "region_enforcement_enabled": "1"},
        follow_redirects=False,
    )
    assert system_config.region_enforcement_enabled() is True
    # Unchecked box -> disabled again.
    admin_ui.post(
        "/ui/settings/system",
        data={"audit_retention_days": "30", "external_user_ttl_days": "60",
              "tasks_enabled": "1"},
        follow_redirects=False,
    )
    assert system_config.region_enforcement_enabled() is False


def test_resolve_se_region_only_when_unambiguous(client: TestClient):
    with client:
        a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
        se = _mk_user("se1", "standard")
        mgr = _mk_user("mgr1", "manager")
        db = get_session_factory()()
        try:
            set_user_regions(db, se, [a])
            set_user_regions(db, mgr, [a, e])
            db.commit()
            assert resolve_se_region_id(db, se) == a       # one region
            assert resolve_se_region_id(db, mgr) is None    # ambiguous
            assert resolve_se_region_id(db, None) is None   # no SE
        finally:
            db.close()


def test_backfill_derives_and_parks_orphans(admin_ui: TestClient):
    a = _mk_region("AMER", 10)
    e = _mk_region("EMEA", 20)
    se = _mk_user("se1", "standard")
    mgr = _mk_user("mgr1", "manager")
    db = get_session_factory()()
    try:
        set_user_regions(db, se, [a])
        set_user_regions(db, mgr, [a, e])  # ambiguous
        db.commit()
        unassigned = db.query(Region).filter_by(is_system=True).one().id
    finally:
        db.close()

    p_se = _mk_project("P-SE", sales_engineer_id=se)
    p_mgr = _mk_project("P-MGR", sales_engineer_id=mgr)
    p_none = _mk_project("P-NONE", sales_engineer_id=None)

    admin_ui.post("/ui/settings/system/backfill-regions", follow_redirects=False)

    db = get_session_factory()()
    try:
        assert db.get(Project, p_se).region_id == a           # derived from SE
        assert db.get(Project, p_mgr).region_id == unassigned  # ambiguous -> parked
        assert db.get(Project, p_none).region_id == unassigned  # no SE -> parked
        # idempotent
        summary = backfill_project_regions(db)
        db.commit()
        assert summary["derived"] == 0  # nothing changes on a second run
        assert db.get(Project, p_se).region_id == a
    finally:
        db.close()
