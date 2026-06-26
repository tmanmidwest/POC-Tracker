"""Tests for global FTS5 search: query sanitization, ranking, trigger sync,
bounds, and the rebuild backstop.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.services import search


@pytest.fixture
def db_session() -> Iterator[Session]:
    from app.db import get_session_factory
    from app.services.migrations import run_migrations

    run_migrations()
    s = get_session_factory()()
    try:
        yield s
    finally:
        s.close()


def _status_id(db: Session) -> int:
    """Ensure at least one project status exists (test DB isn't seeded)."""
    from app.models import ProjectStatus

    row = db.query(ProjectStatus).first()
    if row is None:
        row = ProjectStatus(name="Active", sort_order=1)
        db.add(row)
        db.commit()
    return row.id


def _make_project(db: Session, name: str, notes: str = "") -> int:
    from app.models import Customer, Project

    # Neutral customer name so it doesn't accidentally match project search terms.
    cust = Customer(name=f"Acct {db.query(Customer).count() + 1}")
    db.add(cust)
    db.flush()
    p = Project(customer_id=cust.id, name=name, notes=notes, status_id=_status_id(db))
    db.add(p)
    db.commit()
    return p.id


# ---------------------------------------------------------------------------
# Query sanitization — must never raise into FTS5 MATCH
# ---------------------------------------------------------------------------


def test_build_match_query_basic() -> None:
    assert search.build_match_query("acme ispm") == '"acme" "ispm"*'
    assert search.build_match_query("solo") == '"solo"*'


def test_build_match_query_too_short() -> None:
    assert search.build_match_query("") is None
    assert search.build_match_query(" a ") is None
    assert search.build_match_query(None) is None


@pytest.mark.parametrize(
    "nasty",
    ['"', '*', 'a"b', 'foo AND bar', 'NEAR(x y)', 'a*b(c)', '🔥 emoji', 'col:val', '))) (((', '""'],
)
def test_nasty_input_never_crashes(db_session: Session, nasty: str) -> None:
    # Whatever the input, search() must return a dict and not raise.
    result = search.search(db_session, nasty)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# End-to-end search + trigger sync
# ---------------------------------------------------------------------------


def test_search_finds_project_and_highlights(db_session: Session) -> None:
    _make_project(db_session, "Acme ISPM POC", "posture management pilot")
    groups = db_session and search.search(db_session, "posture")
    assert "project" in groups
    hit = groups["project"][0]
    assert hit.title == "Acme ISPM POC"
    assert "<mark>posture</mark>" in str(hit.subtitle)
    assert hit.url == f"/ui/projects/{hit.id}"


def test_prefix_matches_as_you_type(db_session: Session) -> None:
    _make_project(db_session, "Saviynt Identity Cloud")
    # "ident" should prefix-match "Identity"
    groups = search.search(db_session, "ident")
    assert "project" in groups
    assert any(h.title == "Saviynt Identity Cloud" for h in groups["project"])


def test_insert_update_delete_sync(db_session: Session) -> None:
    from app.models import Project

    pid = _make_project(db_session, "Findable Widget", "alpha")
    assert "project" in search.search(db_session, "Findable")

    # Update: old term gone, new term present.
    p = db_session.get(Project, pid)
    p.name = "Renamed Gadget"
    db_session.commit()
    assert search.search(db_session, "Findable") == {}
    assert "project" in search.search(db_session, "Gadget")

    # Delete: gone from the index.
    db_session.delete(p)
    db_session.commit()
    assert search.search(db_session, "Gadget") == {}


def test_cascade_delete_cleans_child_notes(db_session: Session) -> None:
    from datetime import date

    from app.models import Project, ProjectNote

    pid = _make_project(db_session, "Cascade Co")
    db_session.add(ProjectNote(project_id=pid, note_date=date.today(),
                               body="uniquenoteterm here", created_by="t"))
    db_session.commit()
    assert "note" in search.search(db_session, "uniquenoteterm")

    # Deleting the project cascades to the note; recursive_triggers cleans the index.
    db_session.delete(db_session.get(Project, pid))
    db_session.commit()
    assert search.search(db_session, "uniquenoteterm") == {}


def test_per_type_limit_caps_results(db_session: Session) -> None:
    for i in range(8):
        _make_project(db_session, f"Limited Project {i}")
    groups = search.search(db_session, "Limited", per_type_limit=3)
    assert len(groups["project"]) == 3


def test_rebuild_index_matches_triggers(db_session: Session) -> None:
    _make_project(db_session, "Rebuildable One", "searchable body")
    before = search.search(db_session, "Rebuildable")
    n = search.rebuild_index(db_session)
    assert n >= 1
    after = search.search(db_session, "Rebuildable")
    assert "project" in after
    assert {h.id for h in after["project"]} == {h.id for h in before["project"]}
