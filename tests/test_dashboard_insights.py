"""Tests for the dashboard insight-strip aggregation (KPIs, charts, attention)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def ui(client: TestClient) -> TestClient:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    return client


def _seed(db, *, name, status, uc_specs, end_date=None):
    """Create a project under a fresh customer with the given use-case statuses."""
    from app.models import Customer, Project, ProjectUseCase

    cust = Customer(name=f"Cust {name}")
    db.add(cust)
    db.flush()
    proj = Project(
        customer_id=cust.id, name=name, status_id=status.id, end_date=end_date
    )
    db.add(proj)
    db.flush()
    for i, uc_status in enumerate(uc_specs):
        proj.use_cases.append(
            ProjectUseCase(
                category="General",
                name=f"{name} uc{i}",
                status_id=uc_status.id,
                source="custom",
            )
        )
    db.commit()
    return proj


def _fixtures(db):
    from app.models import AppUser, ProjectStatus, UseCaseStatus
    from app.config import get_settings

    statuses = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).all()
    done = (
        db.query(UseCaseStatus)
        .filter(UseCaseStatus.is_complete_status.is_(True))
        .first()
    )
    todo = (
        db.query(UseCaseStatus)
        .filter(UseCaseStatus.is_complete_status.is_(False))
        .first()
    )
    admin = (
        db.query(AppUser)
        .filter(AppUser.username == get_settings().initial_admin_username)
        .one()
    )
    return statuses, done, todo, admin


def test_kpis_at_risk_and_avg_completion(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.ui.dashboard_routes import _build_insights

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)
    st = statuses[0]

    # Overdue + incomplete → at risk. 1/2 done = 50%.
    overdue = _seed(db, name="Overdue", status=st, uc_specs=[done, todo],
                    end_date=date.today() - timedelta(days=5))
    # Overdue but fully complete → NOT at risk. 100%.
    finished = _seed(db, name="Done", status=st, uc_specs=[done, done],
                     end_date=date.today() - timedelta(days=5))
    # No end date → never at risk. 0%.
    fresh = _seed(db, name="Fresh", status=st, uc_specs=[todo])

    # Scope to just these three, isolating from the seeded sample project.
    ids = {overdue.id, finished.id, fresh.id}
    insights = _build_insights(db, admin, ids, statuses)
    k = insights["kpis"]

    assert k["active"] == 3
    assert k["at_risk"] == 1  # only the overdue+incomplete one
    # Avg of per-project completion: (50 + 100 + 0) / 3 = 50.
    assert k["avg_completion"] == 50

    # The overdue project is surfaced in the attention panel with a reason.
    names = {a["customer"] for a in insights["attention"]}
    assert "Cust Overdue" in names
    assert "Cust Done" not in names
    reasons = insights["attention"][0]["reasons"]
    assert any(r["kind"] == "overdue" for r in reasons)


def test_stalled_detection(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Project
    from app.ui.dashboard_routes import _build_insights

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)

    proj = _seed(db, name="Stale", status=statuses[0], uc_specs=[todo])
    old = datetime.now(timezone.utc) - timedelta(days=30)
    db.query(Project).filter(Project.id == proj.id).update(
        {Project.updated_at: old}
    )
    db.commit()

    insights = _build_insights(db, admin, {proj.id}, statuses)
    assert insights["kpis"]["stalled"] == 1
    assert any(
        r["kind"] == "stalled"
        for a in insights["attention"]
        for r in a["reasons"]
    )


def test_status_series_counts_and_scope(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.ui.dashboard_routes import _build_insights

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)
    st = statuses[0]

    p1 = _seed(db, name="A", status=st, uc_specs=[done])
    p2 = _seed(db, name="B", status=st, uc_specs=[todo])

    # Both of my projects counted under the one status.
    full = _build_insights(db, admin, {p1.id, p2.id}, statuses)
    counts = {s["label"]: s["count"] for s in full["status_series"]}
    assert counts[st.name] == 2

    # Scoped to a single project id: only that one is aggregated.
    scoped = _build_insights(db, admin, {p1.id}, statuses)
    scoped_counts = {s["label"]: s["count"] for s in scoped["status_series"]}
    assert scoped_counts[st.name] == 1
    assert scoped["kpis"]["active"] == 1


def test_empty_scope_has_no_data(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.ui.dashboard_routes import _build_insights

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)

    insights = _build_insights(db, admin, set(), statuses)
    assert insights["has_data"] is False
    assert insights["kpis"]["active"] == 0
    assert insights["status_series"] == []
    assert insights["attention"] == []


def test_dashboard_page_renders_insight_strip(ui: TestClient) -> None:
    from app.db import get_session_factory

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)
    _seed(db, name="Visible", status=statuses[0], uc_specs=[done, todo])

    page = ui.get("/ui/dashboard").text
    assert "Active POCs" in page
    assert "Projects by status" in page
    assert "apexcharts" in page  # chart library is wired up
    assert 'id="insights-data"' in page


def test_kpi_cards_link_to_filtered_views(ui: TestClient) -> None:
    from app.db import get_session_factory

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)
    _seed(db, name="Linkable", status=statuses[0], uc_specs=[done, todo])

    page = ui.get("/ui/dashboard?scope=all").text
    assert "/ui/projects?filter=at_risk" in page
    assert "/ui/projects?filter=stalled" in page
    assert "/ui/reports" in page  # avg-completion card → all-POCs report


def test_projects_list_at_risk_filter(ui: TestClient) -> None:
    from app.db import get_session_factory
    from app.models import Project
    from app.services.insights import is_at_risk

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)

    risky = _seed(db, name="Risky", status=statuses[0], uc_specs=[todo],
                  end_date=date.today() - timedelta(days=4))
    _seed(db, name="Healthy", status=statuses[0], uc_specs=[done],
          end_date=date.today() + timedelta(days=30))

    # Sanity: the predicate agrees with what we seeded.
    assert is_at_risk(db.get(Project, risky.id)) is True

    page = ui.get("/ui/projects?filter=at_risk&scope=all").text
    assert "Cust Risky" in page
    assert "Cust Healthy" not in page
    assert "Filtered · At risk" in page  # active-filter chip shows


def test_projects_list_stalled_filter(ui: TestClient) -> None:
    from datetime import datetime, timezone

    from app.db import get_session_factory
    from app.models import Project

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)

    stale = _seed(db, name="Stale", status=statuses[0], uc_specs=[todo])
    _seed(db, name="Recent", status=statuses[0], uc_specs=[todo])
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db.query(Project).filter(Project.id == stale.id).update(
        {Project.updated_at: old}
    )
    db.commit()

    page = ui.get("/ui/projects?filter=stalled&scope=all").text
    assert "Cust Stale" in page
    assert "Cust Recent" not in page


def test_projects_list_unknown_filter_ignored(ui: TestClient) -> None:
    """A bogus ?filter= value falls back to showing everything, not a 422."""
    from app.db import get_session_factory

    db = get_session_factory()()
    statuses, done, todo, admin = _fixtures(db)
    _seed(db, name="Shown", status=statuses[0], uc_specs=[todo])

    resp = ui.get("/ui/projects?filter=bogus&scope=all")
    assert resp.status_code == 200
    assert "Cust Shown" in resp.text
    assert "Filtered ·" not in resp.text
