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
from app.services.access import accessible_project_ids
from app.services.access import can_view_project as access_can_view
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


def _mk_project(
    name: str,
    sales_engineer_id: int | None = None,
    region_id: int | None = None,
    customer_id: int | None = None,
) -> int:
    db = get_session_factory()()
    try:
        if customer_id is None:
            cust = Customer(name=f"Cust {name}")
            db.add(cust)
            db.flush()
            customer_id = cust.id
        status = db.query(ProjectStatus).first()
        p = Project(
            customer_id=customer_id,
            name=name,
            status_id=status.id,
            sales_engineer_id=sales_engineer_id,
            region_id=region_id,
        )
        db.add(p)
        db.commit()
        return p.id
    finally:
        db.close()


def _enable_enforcement() -> None:
    from app.services import system_config

    db = get_session_factory()()
    try:
        system_config.set_region_enforcement_enabled(db, True)
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


# --- Phase 2: read enforcement ---------------------------------------------


def test_enforcement_off_internal_sees_all(client: TestClient):
    """Default (flag off): a standard SE still sees every project."""
    with client:
        a = _mk_region("AMER", 10)
        se = _mk_user("se1", "standard")
        _mk_project("P-AMER", region_id=a)
        _mk_project("P-OTHER", region_id=None)
        db = get_session_factory()()
        try:
            user = db.get(AppUser, se)
            assert accessible_project_ids(db, user) is None  # None = all
        finally:
            db.close()


def test_accessible_ids_region_scoped(client: TestClient):
    with client:
        a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
        se = _mk_user("se_amer", "standard")
        mgr = _mk_user("mgr", "manager")
        admin = _mk_user("admin2", "admin")
        db = get_session_factory()()
        try:
            set_user_regions(db, se, [a])
            set_user_regions(db, mgr, [a, e])
            db.commit()
        finally:
            db.close()
        p_amer = _mk_project("P-AMER", region_id=a)
        p_emea = _mk_project("P-EMEA", region_id=e)
        p_none = _mk_project("P-NONE", region_id=None)
        p_own = _mk_project("P-OWN", sales_engineer_id=se, region_id=e)  # SE's own, other region
        _enable_enforcement()
        db = get_session_factory()()
        try:
            se_u, mgr_u, admin_u = db.get(AppUser, se), db.get(AppUser, mgr), db.get(AppUser, admin)
            # SE: only AMER projects + their own assignment (even though in EMEA)
            assert accessible_project_ids(db, se_u) == {p_amer, p_own}
            # Manager: both their regions; region-less project excluded
            assert accessible_project_ids(db, mgr_u) == {p_amer, p_emea, p_own}
            # Admin: all (None)
            assert accessible_project_ids(db, admin_u) is None
            # can_view_project (takes Project objects)
            proj = lambda pid: db.get(Project, pid)
            assert access_can_view(db, se_u, proj(p_amer)) is True
            assert access_can_view(db, se_u, proj(p_emea)) is False
            assert access_can_view(db, se_u, proj(p_none)) is False
            assert access_can_view(db, se_u, proj(p_own)) is True   # own assignment
            assert access_can_view(db, admin_u, proj(p_emea)) is True
        finally:
            db.close()


def test_scope_all_intersects_region(client: TestClient):
    """Under enforcement, scope 'all' can't widen past the region boundary."""
    from app.services.scope import SCOPE_ALL, scoped_project_ids

    with client:
        a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
        se = _mk_user("se_amer", "standard")
        db = get_session_factory()()
        try:
            set_user_regions(db, se, [a])
            db.commit()
        finally:
            db.close()
        p_amer = _mk_project("P-AMER", region_id=a)
        _mk_project("P-EMEA", region_id=e)
        _enable_enforcement()
        db = get_session_factory()()
        try:
            se_u = db.get(AppUser, se)
            # 'all' resolves to just the AMER project, not everything
            assert scoped_project_ids(db, se_u, SCOPE_ALL) == {p_amer}
        finally:
            db.close()


def test_detail_and_customer_and_list_enforced(admin_ui: TestClient):
    """End-to-end via HTTP: an SE logged in sees only their region."""
    a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
    se = _mk_user("se_amer", "standard")
    db = get_session_factory()()
    try:
        set_user_regions(db, se, [a])
        db.commit()
        cust = Customer(name="Shared Co")
        db.add(cust)
        db.commit()
        cust_id = cust.id
    finally:
        db.close()
    p_amer = _mk_project("P-AMER", region_id=a, customer_id=cust_id)
    p_emea = _mk_project("P-EMEA", region_id=e, customer_id=cust_id)
    _enable_enforcement()

    # log in as the SE (admin_ui already logged in as admin; use a fresh client)
    from app.main import create_app
    from fastapi.testclient import TestClient as TC

    app = create_app()
    with TC(app) as se_client:
        r = se_client.post(
            "/ui/login", data={"username": "se_amer", "password": "password123"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        # detail of own-region project: 200; other region: 404
        assert se_client.get(f"/ui/projects/{p_amer}").status_code == 200
        assert se_client.get(f"/ui/projects/{p_emea}").status_code == 404
        # customer detail lists only the AMER project
        page = se_client.get(f"/ui/customers/{cust_id}").text
        assert "P-AMER" in page and "P-EMEA" not in page
        # project list shows only AMER
        lst = se_client.get("/ui/projects?scope=all").text
        assert "P-AMER" in lst and "P-EMEA" not in lst


# --- Phase 3: write enforcement --------------------------------------------


def _se_client(username: str):
    from app.main import create_app
    from fastapi.testclient import TestClient as TC

    app = create_app()
    client = TC(app)
    client.__enter__()
    r = client.post(
        "/ui/login", data={"username": username, "password": "password123"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


def test_write_guard_blocks_out_of_region_mutation(admin_ui: TestClient):
    a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
    se = _mk_user("se_amer", "standard")
    db = get_session_factory()()
    try:
        set_user_regions(db, se, [a])
        db.commit()
    finally:
        db.close()
    p_amer = _mk_project("P-AMER", sales_engineer_id=se, region_id=a)
    p_emea = _mk_project("P-EMEA", region_id=e)
    _enable_enforcement()

    c = _se_client("se_amer")
    try:
        # Archiving an out-of-region project 404s (can't even load it)
        assert c.post(f"/ui/projects/{p_emea}/archive", follow_redirects=False).status_code == 404
        # Editing own-region project works (303 redirect)
        assert c.get(f"/ui/projects/{p_amer}/edit").status_code == 200
        # Out-of-region edit form 404s
        assert c.get(f"/ui/projects/{p_emea}/edit").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_create_syncs_region_from_se(admin_ui: TestClient):
    """A region-scoped SE creating a POC lands it in their region automatically."""
    a = _mk_region("AMER", 10)
    se = _mk_user("se_amer", "standard")
    db = get_session_factory()()
    try:
        set_user_regions(db, se, [a])
        db.commit()
        cust = Customer(name="Acme")
        db.add(cust)
        db.commit()
        cust_id, status_id = cust.id, db.query(ProjectStatus).first().id
    finally:
        db.close()
    _enable_enforcement()

    c = _se_client("se_amer")
    try:
        r = c.post(
            "/ui/projects/new",
            data={"customer_id": str(cust_id), "name": "New AMER POC",
                  "status_id": str(status_id), "sales_engineer_id": str(se)},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
    finally:
        c.__exit__(None, None, None)
    db = get_session_factory()()
    try:
        p = db.query(Project).filter_by(name="New AMER POC").one()
        assert p.region_id == a  # auto-derived from the SE
    finally:
        db.close()


def test_subresource_writes_blocked_out_of_region(admin_ui: TestClient):
    """Top-level use-case/note routes (addressed by their own id) still enforce
    the parent project's region."""
    from datetime import date

    from app.models import ProjectNote, ProjectUseCase, UseCaseStatus

    a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
    se = _mk_user("se_amer", "standard")
    db = get_session_factory()()
    try:
        set_user_regions(db, se, [a])
        db.commit()
    finally:
        db.close()
    p_emea = _mk_project("P-EMEA", region_id=e)
    # a use case + note on the out-of-region project
    db = get_session_factory()()
    try:
        st = db.query(UseCaseStatus).first()
        uc = ProjectUseCase(project_id=p_emea, category="C", name="UC", status_id=st.id)
        note = ProjectNote(project_id=p_emea, body="hi", note_date=date.today())
        db.add_all([uc, note])
        db.commit()
        uc_id, note_id, status_id = uc.id, note.id, st.id
    finally:
        db.close()
    _enable_enforcement()

    c = _se_client("se_amer")
    try:
        # quick status change on an out-of-region use case -> 404
        r = c.post(f"/ui/projects/use-cases/{uc_id}/status",
                   data={"status_id": str(status_id)}, follow_redirects=False)
        assert r.status_code == 404
        # deleting an out-of-region note -> 404
        r = c.post(f"/ui/projects/notes/{note_id}/delete", follow_redirects=False)
        assert r.status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_region_column_filter_and_rollup(admin_ui: TestClient):
    """Phase 5: region column + filter on the project list, region rollup in
    analytics."""
    a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
    p_amer = _mk_project("A-Win", region_id=a)
    _mk_project("E-Deal", region_id=e)

    # list: region column + filter present, and the filter narrows results
    page = admin_ui.get("/ui/projects?view=all&scope=all").text
    assert ">Region<" in page and 'name="region_id"' in page
    assert "A-Win" in page and "E-Deal" in page
    filtered = admin_ui.get(f"/ui/projects?view=all&scope=all&region_id={a}").text
    assert "A-Win" in filtered and "E-Deal" not in filtered

    # analytics: By Region rollup lists both regions
    an = admin_ui.get("/ui/reports/analytics").text
    assert "By Region" in an and "AMER" in an and "EMEA" in an


def test_can_grant_region_aware(client: TestClient):
    from app.services.access import can_grant_project

    with client:
        a, e = _mk_region("AMER", 10), _mk_region("EMEA", 20)
        mgr = _mk_user("mgr", "manager")
        se_other = _mk_user("se_other", "standard")
        db = get_session_factory()()
        try:
            set_user_regions(db, mgr, [a])
            db.commit()
        finally:
            db.close()
        # project in AMER owned by someone else
        p_amer = _mk_project("P-AMER", sales_engineer_id=se_other, region_id=a)
        p_emea = _mk_project("P-EMEA", sales_engineer_id=se_other, region_id=e)
        _enable_enforcement()
        db = get_session_factory()()
        try:
            mgr_u = db.get(AppUser, mgr)
            # manager can grant on an in-region project they don't own
            assert can_grant_project(db, mgr_u, db.get(Project, p_amer)) is True
            # but not an out-of-region one
            assert can_grant_project(db, mgr_u, db.get(Project, p_emea)) is False
        finally:
            db.close()
