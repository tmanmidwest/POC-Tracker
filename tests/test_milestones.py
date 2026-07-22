"""POC milestones: the lifecycle timeline, its health signal, and templating.

Covers seeding from the default set, the overdue/off-track derivation shared by
the dashboard and project list, the timeline CRUD routes, and the template →
wizard → project offset round-trip.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    Customer,
    MilestoneDefault,
    Project,
    ProjectMilestone,
    ProjectStatus,
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


_counter = [0]


def _make_project(db, *, start: date | None = None, status_name: str = "In Progress") -> Project:
    _counter[0] += 1
    status = db.query(ProjectStatus).filter_by(name=status_name).one()
    cust = Customer(name=f"MsCo {_counter[0]}")
    db.add(cust)
    db.flush()
    proj = Project(
        customer_id=cust.id,
        name=f"MS-{cust.id}",
        status_id=status.id,
        start_date=start,
    )
    db.add(proj)
    db.flush()
    return proj


# ---------------------------------------------------------------------------
# Seeding from blueprints
# ---------------------------------------------------------------------------


def test_default_set_is_seeded(client: TestClient) -> None:
    """The standard lifecycle ships as an admin-editable default set."""
    with client:
        db = get_session_factory()()
        try:
            rows = db.query(MilestoneDefault).order_by(MilestoneDefault.sort_order).all()
            assert [r.name for r in rows] == [
                "Kickoff",
                "Success Criteria Agreed",
                "Mid-point Check",
                "Readout",
            ]
            assert [r.target_offset_days for r in rows] == [0, 3, 14, 28]
        finally:
            db.close()


def test_seed_project_milestones_anchors_to_start_date(client: TestClient) -> None:
    """Offsets resolve against the project's start date."""
    from app.services.milestones import seed_project_milestones

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            added = seed_project_milestones(db, p)
            db.commit()
            assert added == 4
            by_name = {m.name: m.target_date for m in p.milestones}
            assert by_name["Kickoff"] == date(2026, 3, 1)
            assert by_name["Success Criteria Agreed"] == date(2026, 3, 4)
            assert by_name["Readout"] == date(2026, 3, 29)
        finally:
            db.close()


def test_seed_without_start_date_leaves_milestones_undated(client: TestClient) -> None:
    """No start date → milestones exist but carry no target (never overdue)."""
    from app.services.insights import is_off_track
    from app.services.milestones import seed_project_milestones

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=None)
            seed_project_milestones(db, p)
            db.commit()
            assert len(p.milestones) == 4
            assert all(m.target_date is None for m in p.milestones)
            assert is_off_track(p, date(2030, 1, 1)) is False
        finally:
            db.close()


def test_seeding_is_idempotent(client: TestClient) -> None:
    """Re-seeding never duplicates an existing timeline."""
    from app.services.milestones import seed_project_milestones

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            assert seed_project_milestones(db, p) == 4
            db.commit()
            assert seed_project_milestones(db, p) == 0
            db.commit()
            assert len(p.milestones) == 4
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Health signal
# ---------------------------------------------------------------------------


def test_overdue_and_off_track(client: TestClient) -> None:
    from app.services.insights import is_off_track, next_milestone, overdue_milestones

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            p.milestones.append(ProjectMilestone(name="A", target_date=date(2026, 3, 1), sort_order=10))
            p.milestones.append(ProjectMilestone(name="B", target_date=date(2026, 4, 1), sort_order=20))
            db.commit()

            today = date(2026, 3, 15)
            assert [m.name for m in overdue_milestones(p, today)] == ["A"]
            assert is_off_track(p, today) is True
            assert next_milestone(p, today).name == "A"

            # Completing the overdue one clears the signal and advances the pointer.
            p.milestones[0].completed_date = today
            db.commit()
            assert is_off_track(p, today) is False
            assert next_milestone(p, today).name == "B"
        finally:
            db.close()


def test_closed_projects_are_never_off_track(client: TestClient) -> None:
    """A finished POC isn't 'off track' for a milestone it never closed out."""
    from app.services.insights import is_off_track

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1), status_name="Completed - Won")
            p.milestones.append(
                ProjectMilestone(name="Readout", target_date=date(2026, 3, 2), sort_order=10)
            )
            db.commit()
            assert is_off_track(p, date(2026, 6, 1)) is False
        finally:
            db.close()


def test_milestone_progress_tally(client: TestClient) -> None:
    from app.services.insights import milestone_progress

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db)
            p.milestones.append(ProjectMilestone(name="A", sort_order=10, completed_date=date(2026, 3, 1)))
            p.milestones.append(ProjectMilestone(name="B", sort_order=20))
            db.commit()
            assert milestone_progress(p) == {"total": 2, "done": 1, "pct": 50}
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Timeline UI routes
# ---------------------------------------------------------------------------


def test_timeline_crud_roundtrip(admin_ui: TestClient) -> None:
    db = get_session_factory()()
    try:
        p = _make_project(db, start=date(2026, 3, 1))
        db.commit()
        pid = p.id
    finally:
        db.close()

    # Apply the standard set, then add a custom milestone.
    assert admin_ui.post(f"/ui/projects/{pid}/milestones/apply-defaults", follow_redirects=False).status_code == 303
    resp = admin_ui.post(
        f"/ui/projects/{pid}/milestones",
        data={"name": "Security Review", "target_date": "2026-04-15", "notes": "with InfoSec"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = admin_ui.get(f"/ui/projects/{pid}")
    assert 'id="tab-timeline"' in page.text
    assert "Security Review" in page.text

    db = get_session_factory()()
    try:
        rows = (
            db.query(ProjectMilestone)
            .filter_by(project_id=pid)
            .order_by(ProjectMilestone.sort_order)
            .all()
        )
        assert [r.name for r in rows][-1] == "Security Review"
        first_id, second_id = rows[0].id, rows[1].id
        first_name, second_name = rows[0].name, rows[1].name
    finally:
        db.close()

    # Complete, reorder, then delete.
    admin_ui.post(f"/ui/projects/{pid}/milestones/{first_id}/complete", data={"complete": "1"})
    admin_ui.post(f"/ui/projects/{pid}/milestones/{first_id}/move", data={"direction": "down"})
    admin_ui.post(f"/ui/projects/{pid}/milestones/{second_id}/delete")

    db = get_session_factory()()
    try:
        rows = (
            db.query(ProjectMilestone)
            .filter_by(project_id=pid)
            .order_by(ProjectMilestone.sort_order)
            .all()
        )
        names = [r.name for r in rows]
        assert second_name not in names  # deleted
        done = next(r for r in rows if r.name == first_name)
        assert done.completed_date is not None  # completed
    finally:
        db.close()


def test_apply_defaults_refuses_to_duplicate(admin_ui: TestClient) -> None:
    db = get_session_factory()()
    try:
        p = _make_project(db, start=date(2026, 3, 1))
        db.commit()
        pid = p.id
    finally:
        db.close()

    admin_ui.post(f"/ui/projects/{pid}/milestones/apply-defaults")
    admin_ui.post(f"/ui/projects/{pid}/milestones/apply-defaults")

    db = get_session_factory()()
    try:
        assert db.query(ProjectMilestone).filter_by(project_id=pid).count() == 4
    finally:
        db.close()


def test_milestones_cascade_on_project_delete(client: TestClient) -> None:
    from app.services.milestones import seed_project_milestones

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            seed_project_milestones(db, p)
            db.commit()
            pid = p.id
            db.delete(p)
            db.commit()
            assert db.query(ProjectMilestone).filter_by(project_id=pid).count() == 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Dashboard + list integration
# ---------------------------------------------------------------------------


def test_off_track_kpi_and_list_filter(admin_ui: TestClient) -> None:
    db = get_session_factory()()
    try:
        p = _make_project(db, start=date(2026, 3, 1))
        p.milestones.append(
            ProjectMilestone(name="Overdue thing", target_date=date(2020, 1, 1), sort_order=10)
        )
        db.commit()
        name = p.name
    finally:
        db.close()

    dash = admin_ui.get("/ui/dashboard?scope=all")
    assert "Off track" in dash.text
    assert "filter=off_track" in dash.text

    listing = admin_ui.get("/ui/projects?filter=off_track&scope=all")
    assert listing.status_code == 200
    assert name in listing.text


# ---------------------------------------------------------------------------
# Template round-trip
# ---------------------------------------------------------------------------


def test_template_snapshot_and_reanchor(client: TestClient) -> None:
    """Milestones snapshot as offsets and re-anchor onto a new start date."""
    from app.services.poc_templates import (
        create_template_from_project,
        template_to_wizard_context,
    )

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            p.milestones.append(ProjectMilestone(name="Kickoff", target_date=date(2026, 3, 1), sort_order=10))
            p.milestones.append(ProjectMilestone(name="Readout", target_date=date(2026, 3, 29), sort_order=20))
            db.commit()

            tpl = create_template_from_project(db, p, name="RoundTrip", created_by="t")
            db.commit()
            assert [(m.name, m.target_offset_days) for m in tpl.milestones] == [
                ("Kickoff", 0),
                ("Readout", 28),
            ]

            ctx = template_to_wizard_context(db, tpl, base_date=date(2026, 9, 1))
            assert ctx["milestone_rows"] == [
                {"name": "Kickoff", "target_date": "2026-09-01"},
                {"name": "Readout", "target_date": "2026-09-29"},
            ]
        finally:
            db.close()


def test_undated_milestone_snapshots_as_null_offset(client: TestClient) -> None:
    from app.services.poc_templates import create_template_from_project

    with client:
        db = get_session_factory()()
        try:
            p = _make_project(db, start=date(2026, 3, 1))
            p.milestones.append(ProjectMilestone(name="Someday", target_date=None, sort_order=10))
            db.commit()
            tpl = create_template_from_project(db, p, name="Undated", created_by="t")
            db.commit()
            assert tpl.milestones[0].target_offset_days is None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Wizard integration
# ---------------------------------------------------------------------------


def test_wizard_creates_submitted_milestones(admin_ui: TestClient) -> None:
    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={
            "customer_mode": "new",
            "new_customer_name": "WizardMs Co",
            "name": "WZ Timeline",
            "start_date": "2026-09-01",
            "milestone_name": ["Kickoff", "Readout"],
            "milestone_date": ["2026-09-01", "2026-09-29"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        p = db.query(Project).filter_by(name="WZ Timeline").one()
        assert [(m.name, m.target_date) for m in p.milestones] == [
            ("Kickoff", date(2026, 9, 1)),
            ("Readout", date(2026, 9, 29)),
        ]
    finally:
        db.close()


def test_wizard_with_no_milestone_rows_creates_none(admin_ui: TestClient) -> None:
    """Clearing every row is a deliberate 'no timeline', not a reset to defaults."""
    resp = admin_ui.post(
        "/ui/projects/wizard",
        data={
            "customer_mode": "new",
            "new_customer_name": "NoMs Co",
            "name": "WZ Empty",
            "start_date": "2026-09-01",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        p = db.query(Project).filter_by(name="WZ Empty").one()
        assert p.milestones == []
    finally:
        db.close()


def test_plain_create_form_seeds_standard_set(admin_ui: TestClient) -> None:
    """A POC made from the ordinary form still starts with a timeline."""
    db = get_session_factory()()
    try:
        cust = Customer(name="FormMs Co")
        db.add(cust)
        db.commit()
        cid = cust.id
    finally:
        db.close()

    resp = admin_ui.post(
        "/ui/projects/new",
        data={"customer_id": str(cid), "name": "Form POC", "start_date": "2026-05-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        p = db.query(Project).filter_by(name="Form POC").one()
        assert [m.name for m in p.milestones] == [
            "Kickoff",
            "Success Criteria Agreed",
            "Mid-point Check",
            "Readout",
        ]
        assert p.milestones[0].target_date == date(2026, 5, 1)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Admin lookup
# ---------------------------------------------------------------------------


def test_milestone_defaults_lookup_ui(admin_ui: TestClient) -> None:
    page = admin_ui.get("/ui/lookups/milestone-defaults")
    assert page.status_code == 200
    assert "Kickoff" in page.text


def test_blank_offset_saves_as_null(admin_ui: TestClient) -> None:
    """A blank offset means 'undated', not the numeric-field default."""
    db = get_session_factory()()
    try:
        row = db.query(MilestoneDefault).filter_by(name="Readout").one()
        rid = row.id
    finally:
        db.close()

    resp = admin_ui.post(
        f"/ui/lookups/milestone-defaults/{rid}/edit",
        data={"name": "Readout", "target_offset_days": "", "sort_order": "40", "is_active": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        assert db.get(MilestoneDefault, rid).target_offset_days is None
    finally:
        db.close()
