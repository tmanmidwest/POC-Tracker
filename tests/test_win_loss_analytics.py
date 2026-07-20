"""Tests for win/loss outcome tracking and portfolio analytics.

Covers the derivation of outcome from status, cycle-time math, the
``portfolio_stats`` aggregator, the REST analytics endpoint, the close-reasons
lookup, and the migration backfill that maps the seeded Won/Lost statuses.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _statuses(db):
    from app.models import ProjectStatus

    rows = {s.name: s for s in db.query(ProjectStatus).all()}
    return rows["Completed - Won"], rows["Completed - Lost"], rows["In Progress"]


_counter = [0]


def _make(db, *, status, start=None, closed=None, se_id=None, reason_id=None, competitor=None):
    from app.models import Customer, Project

    _counter[0] += 1
    cust = Customer(name=f"Cust {_counter[0]}")
    db.add(cust)
    db.flush()
    proj = Project(
        customer_id=cust.id,
        name=f"P-{cust.id}",
        status_id=status.id,
        start_date=start,
        closed_date=closed,
        sales_engineer_id=se_id,
        close_reason_id=reason_id,
        competitor=competitor,
    )
    db.add(proj)
    db.flush()
    return proj


# ---------------------------------------------------------------------------
# Outcome derivation + cycle time
# ---------------------------------------------------------------------------


def test_seeded_status_outcomes(client: TestClient) -> None:
    """The seeded terminal statuses carry the right structured outcome."""
    with client:  # trigger lifespan → migrate + seed
        from app.db import get_session_factory

        db = get_session_factory()()
        won, lost, in_prog = _statuses(db)
        assert won.outcome == "won"
        assert lost.outcome == "lost"
        assert in_prog.outcome == "none"
        assert won.is_terminal and lost.is_terminal
        assert not in_prog.is_terminal


def test_outcome_and_cycle_time_helpers(client: TestClient) -> None:
    with client:
        from app.db import get_session_factory
        from app.services import insights

        db = get_session_factory()()
        won, lost, in_prog = _statuses(db)

        p_won = _make(db, status=won, start=date(2026, 1, 1), closed=date(2026, 1, 31))
        p_open = _make(db, status=in_prog, start=date(2026, 1, 1))

        assert insights.outcome(p_won) == "won"
        assert insights.is_won(p_won) and insights.is_closed(p_won)
        assert insights.is_open(p_open) and not insights.is_closed(p_open)
        assert insights.cycle_time_days(p_won) == 30
        # Open project has no closed_date → no cycle time.
        assert insights.cycle_time_days(p_open) is None


def test_win_rate_excludes_no_decision() -> None:
    from app.services import insights

    # 3 won, 1 lost → 75%. no_decision never enters the denominator.
    assert insights.win_rate(3, 1) == 75.0
    assert insights.win_rate(0, 0) is None


# ---------------------------------------------------------------------------
# portfolio_stats aggregation
# ---------------------------------------------------------------------------


def test_portfolio_stats(client: TestClient) -> None:
    with client:
        from app.db import get_session_factory
        from app.models import AppUser, CloseReason
        from app.services import insights

        db = get_session_factory()()
        won, lost, in_prog = _statuses(db)
        se = db.query(AppUser).first()
        reason = db.query(CloseReason).filter_by(name="Chose competitor").one()

        # 2 won, 2 lost, 1 open.
        _make(db, status=won, start=date(2026, 1, 1), closed=date(2026, 1, 31), se_id=se.id)  # 30d
        _make(db, status=won, start=date(2026, 1, 1), closed=date(2026, 2, 20))  # 50d
        _make(db, status=lost, start=date(2026, 2, 1), closed=date(2026, 3, 3),
              reason_id=reason.id, competitor="Okta")  # 30d
        _make(db, status=lost, start=date(2026, 2, 1), closed=date(2026, 2, 21),
              competitor="Okta")  # 20d, no reason
        _make(db, status=in_prog, start=date(2026, 3, 1))
        db.commit()

        # Analytics scoped to just the projects we created (exclude the seeded sample).
        from app.models import Project

        mine = [p for p in db.query(Project).all() if p.name.startswith("P-")]
        s = insights.portfolio_stats(mine)

        assert s["won"] == 2
        assert s["lost"] == 2
        assert s["open"] == 1
        assert s["decided"] == 4
        assert s["win_rate"] == 50.0
        # cycle times: (30, 50, 30, 20) → mean 32.5; wins (30, 50) → 40
        assert s["avg_cycle_time_days"] == 32.5
        assert s["avg_cycle_time_won_days"] == 40.0

        # Loss reasons: one "Chose competitor", one "Unspecified".
        reasons = {r["label"]: r["count"] for r in s["loss_reasons"]}
        assert reasons == {"Chose competitor": 1, "Unspecified": 1}
        # Competitor Okta appears on both losses.
        competitors = {r["label"]: r["count"] for r in s["competitors"]}
        assert competitors == {"Okta": 2}


def test_portfolio_stats_empty() -> None:
    from app.services import insights

    s = insights.portfolio_stats([])
    assert s["total"] == 0
    assert s["win_rate"] is None
    assert s["avg_cycle_time_days"] is None
    assert s["by_sales_engineer"] == []


# ---------------------------------------------------------------------------
# REST endpoint + lookup CRUD
# ---------------------------------------------------------------------------


def test_analytics_endpoint(api_client: TestClient) -> None:
    r = api_client.get("/api/v1/projects/analytics/win-loss")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("won", "lost", "open", "win_rate", "by_sales_engineer", "loss_reasons"):
        assert key in body


def test_close_reasons_lookup_crud(api_client: TestClient) -> None:
    # Seeded defaults are present.
    r = api_client.get("/api/v1/close-reasons/")
    assert r.status_code == 200
    names = {row["name"] for row in r.json()}
    assert "Chose competitor" in names

    # Create + delete a custom one.
    r = api_client.post("/api/v1/close-reasons/", json={"name": "Legal review"})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = api_client.delete(f"/api/v1/close-reasons/{rid}")
    assert r.status_code == 204


def test_project_status_outcome_via_api(api_client: TestClient) -> None:
    """A new terminal status can carry an outcome through the lookup API."""
    r = api_client.post(
        "/api/v1/project-statuses/",
        json={"name": "Closed - Churned", "is_terminal": True, "outcome": "lost"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "lost"


# ---------------------------------------------------------------------------
# Migration backfill
# ---------------------------------------------------------------------------


def test_backfill_stamps_closed_date_for_terminal_projects(client: TestClient) -> None:
    """A project sitting in a terminal status gets a closed_date auto-stamped
    when saved through the UI, so cycle-time has a date without SE effort."""
    with client:
        from app.db import get_session_factory
        from app.models import Customer, Project
        from app.ui.project_routes import _apply_close_details

        db = get_session_factory()()
        won, _lost, _in_prog = _statuses(db)
        cust = Customer(name="Backfill Co")
        db.add(cust)
        db.flush()
        proj = Project(customer_id=cust.id, name="BF", status_id=won.id)
        db.add(proj)
        db.flush()

        # No closed_date entered, terminal status → stamped to today.
        _apply_close_details(
            proj, {"close_reason_id": None, "competitor": None, "closed_date": None}, db
        )
        assert proj.closed_date == date.today()
