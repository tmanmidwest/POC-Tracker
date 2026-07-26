"""Tests for the in-app reporting layer (registry, engine, saved reports, routes).

The security-critical assertions are region scope (a region-scoped SE only ever
sees their own rows, no matter the report) and the definition whitelist (an
off-registry field/operator is rejected before any query runs).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    AppUser,
    Customer,
    Project,
    ProjectStatus,
    ProjectUseCase,
    Region,
    SavedReport,
    UseCaseStatus,
    UserRegion,
)
from app.services import system_config
from app.services.passwords import hash_password
from app.services.reporting import engine, saved
from app.services.reporting import registry as reg


# --------------------------------------------------------------------------- #
# Registry (pure)
# --------------------------------------------------------------------------- #


def test_registry_validates_tabular_and_summary() -> None:
    d = reg.validate_definition(
        "project",
        {
            "columns": ["customer", "status", "completion_pct"],
            "filters": [{"field": "region", "op": "in", "value": ["EMEA"]}],
            "sort": [{"field": "completion_pct", "dir": "desc"}],
        },
        summary=False,
    )
    assert d["columns"] == ["customer", "status", "completion_pct"]
    assert d["sort"] == [{"field": "completion_pct", "dir": "desc"}]

    s = reg.validate_definition(
        "project",
        {"group_by": "sales_engineer", "measures": [{"fn": "avg", "field": "completion_pct"}]},
        summary=True,
    )
    assert s["group_by"] == "sales_engineer"
    assert s["measures"][0]["fn"] == "avg"


@pytest.mark.parametrize(
    "definition, summary",
    [
        ({"columns": ["nope"]}, False),                                    # unknown field
        ({"columns": ["status"], "filters": [{"field": "status", "op": "contains", "value": "x"}]}, False),  # bad op
        ({"columns": ["status"], "group_by": "name"}, True),               # non-groupable group_by
        ({"measures": [{"fn": "avg", "field": "status"}]}, True),          # non-numeric measure
        ({}, True),                                                        # summary with nothing
        ({"columns": []}, False),                                          # tabular with no columns
    ],
)
def test_registry_rejects_off_registry(definition, summary) -> None:
    with pytest.raises(reg.DefinitionError):
        reg.validate_definition("project", definition, summary=summary)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


def _mk_user(username: str, role: str, email: str | None = None) -> int:
    db = get_session_factory()()
    try:
        u = AppUser(username=username, email=email, password_hash=hash_password("password123"), is_active=True)
        u.role = role
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _mk_region(name: str) -> int:
    db = get_session_factory()()
    try:
        r = Region(name=name, sort_order=100, is_active=True, is_system=False)
        db.add(r)
        db.commit()
        return r.id
    finally:
        db.close()


def _assign_region(user_id: int, region_id: int) -> None:
    db = get_session_factory()()
    try:
        db.add(UserRegion(user_id=user_id, region_id=region_id))
        db.commit()
    finally:
        db.close()


def _mk_project(name: str, *, se_id=None, region_id=None, complete_uc=0, total_uc=0) -> int:
    db = get_session_factory()()
    try:
        cust = Customer(name=f"Cust {name}")
        db.add(cust)
        db.flush()
        status = db.query(ProjectStatus).first()
        p = Project(customer_id=cust.id, name=name, status_id=status.id,
                    sales_engineer_id=se_id, region_id=region_id)
        db.add(p)
        db.flush()
        done = db.query(UseCaseStatus).filter(UseCaseStatus.is_complete_status.is_(True)).first()
        todo = db.query(UseCaseStatus).filter(UseCaseStatus.is_complete_status.is_(False)).first()
        for i in range(total_uc):
            st = done if i < complete_uc else todo
            db.add(ProjectUseCase(project_id=p.id, category="Cat", name=f"UC{i}", status_id=st.id))
        db.commit()
        return p.id
    finally:
        db.close()


def _run(user_id: int, entity: str, definition: dict, *, is_summary=False):
    db = get_session_factory()()
    try:
        user = db.get(AppUser, user_id)
        clean = reg.validate_definition(entity, definition, summary=is_summary)
        return engine.run(db, user, entity, clean, is_summary=is_summary)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Engine — region scope (the security boundary)
# --------------------------------------------------------------------------- #


def test_engine_region_scopes_rows(client) -> None:  # client seeds the DB
    emea = _mk_region("EMEA")
    amer = _mk_region("AMER")
    se = _mk_user("se_emea", "standard", "se@x.com")
    admin_id = _mk_user("admin2", "admin", "a2@x.com")
    _assign_region(se, emea)
    _mk_project("EMEA-1", se_id=se, region_id=emea)
    _mk_project("EMEA-2", region_id=emea)
    _mk_project("AMER-1", region_id=amer)

    db = get_session_factory()()
    try:
        system_config.set_region_enforcement_enabled(db, True)
    finally:
        db.close()

    cols = {"columns": ["name", "region"]}
    admin_res = _run(admin_id, "project", cols)
    se_res = _run(se, "project", cols)

    admin_names = {r.cells[0] for r in admin_res.rows}
    se_names = {r.cells[0] for r in se_res.rows}
    # Admin sees every region (plus any seeded sample project).
    assert {"EMEA-1", "EMEA-2", "AMER-1"} <= admin_names
    # SE sees only their region ∪ own assignment — never the AMER project.
    assert {"EMEA-1", "EMEA-2"} <= se_names
    assert "AMER-1" not in se_names
    assert {r.cells[1] for r in se_res.rows} == {"EMEA"}


def test_engine_derived_filter_and_summary(client) -> None:
    se = _mk_user("se1", "manager", "m@x.com")
    _mk_project("P-hi", se_id=se, total_uc=4, complete_uc=4)   # 100%
    _mk_project("P-lo", se_id=se, total_uc=4, complete_uc=1)   # 25%
    # Scope every query to this SE so the seeded sample project can't interfere.
    mine = {"field": "sales_engineer", "op": "eq", "value": "se1"}

    # Derived filter: completion_pct >= 50 keeps only the finished project.
    res = _run(se, "project", {
        "columns": ["name", "completion_pct"],
        "filters": [mine, {"field": "completion_pct", "op": "gte", "value": 50}],
    })
    assert res.total_matched == 1
    assert res.rows[0].cells[0] == "P-hi"

    # Summary: avg completion across both of this SE's projects.
    summ = _run(se, "project", {
        "filters": [mine],
        "group_by": "sales_engineer",
        "measures": [{"fn": "avg", "field": "completion_pct"}],
    }, is_summary=True)
    assert len(summ.group_rows) == 1
    assert summ.group_rows[0].count == 2
    # avg of 100 and 25 = 62.5 -> shown with a % suffix
    assert summ.group_rows[0].measures[0].endswith("%")


# --------------------------------------------------------------------------- #
# Saved reports — CRUD + visibility
# --------------------------------------------------------------------------- #


def test_saved_report_visibility(client) -> None:
    owner_id = _mk_user("owner", "manager", "o@x.com")
    other_id = _mk_user("other", "manager", "ot@x.com")
    region = _mk_region("EMEA")
    _assign_region(other_id, region)

    db = get_session_factory()()
    try:
        owner = db.get(AppUser, owner_id)
        # private
        priv = saved.create_report(
            db, owner, name="Priv", description=None, entity="project",
            is_summary=False, definition={"columns": ["name"]},
        )
        # shared to EMEA region
        shared = saved.create_report(
            db, owner, name="SharedEMEA", description=None, entity="project",
            is_summary=False, definition={"columns": ["name"]},
            visibility="shared", audience={"role_keys": [], "region_ids": [region]},
        )
        # published
        pub = saved.create_report(
            db, owner, name="Pub", description=None, entity="project",
            is_summary=False, definition={"columns": ["name"]},
            visibility="published", audience={"kind": "internal"},
        )
        other = db.get(AppUser, other_id)
        assert saved.can_view_report(db, other, priv) is False       # private → hidden
        assert saved.can_view_report(db, other, shared) is True       # region audience match
        assert saved.can_view_report(db, other, pub) is True          # published → all internal
        assert saved.can_edit_report(other, priv) is False            # not owner
        assert saved.can_edit_report(owner, priv) is True

        visible = {r.name for r in saved.visible_reports(db, other)}
        assert visible == {"SharedEMEA", "Pub"}
    finally:
        db.close()


def test_saved_report_rejects_bad_definition(client) -> None:
    owner_id = _mk_user("owner2", "manager", "o2@x.com")
    db = get_session_factory()()
    try:
        owner = db.get(AppUser, owner_id)
        with pytest.raises(ValueError):
            saved.create_report(
                db, owner, name="Bad", description=None, entity="project",
                is_summary=False, definition={"columns": ["not_a_field"]},
            )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Routes — smoke + capability gate
# --------------------------------------------------------------------------- #


def _login(client: TestClient, username: str, password: str = "password123") -> None:
    resp = client.post("/ui/login", data={"username": username, "password": password}, follow_redirects=False)
    assert resp.status_code == 303, resp.text


def test_routes_end_to_end(admin_session) -> None:
    c = admin_session
    # List page
    assert c.get("/ui/reports/saved").status_code == 200
    # Builder page
    assert c.get("/ui/reports/new").status_code == 200
    # Preview partial
    import json
    prev = c.post("/ui/reports/preview", data={
        "entity": "project", "is_summary": "0",
        "definition_json": json.dumps({"columns": ["name", "status"]}),
    })
    assert prev.status_code == 200
    # Create
    created = c.post("/ui/reports/", data={
        "name": "Smoke report", "description": "",
        "entity": "project", "is_summary": "0", "visibility": "private",
        "definition_json": json.dumps({"columns": ["name", "status"]}),
    }, follow_redirects=False)
    assert created.status_code == 303
    rid = created.headers["location"].split("/")[-2]
    # Run + export
    assert c.get(f"/ui/reports/{rid}/run").status_code == 200
    csv = c.get(f"/ui/reports/{rid}/export.csv")
    assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_external_user_cannot_reach_builder(client) -> None:
    _mk_user("ext1", "external", "ext@x.com")
    _login(client, "ext1")
    # report.create is internal-tier; an external viewer is bounced (redirect to dashboard).
    resp = client.get("/ui/reports/new", follow_redirects=False)
    assert resp.status_code in (302, 303)
