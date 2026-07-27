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


def _make_feedback(db: Session, title: str, body: str = "",
                   kind: str = "feature_request") -> int:
    from app.models import Feedback, FeedbackStatus

    status = db.query(FeedbackStatus).first()
    if status is None:
        status = FeedbackStatus(
            name="New", sort_order=1, is_terminal=False, is_active=True, is_system=True
        )
        db.add(status)
        db.flush()
    f = Feedback(
        submitter_user_id=None,
        submitter_label="Tester",
        kind=kind,
        title=title,
        body=body or None,
        status_id=status.id,
    )
    db.add(f)
    db.commit()
    return f.id


# ---------------------------------------------------------------------------
# Query sanitization — must never raise into FTS5 MATCH
# ---------------------------------------------------------------------------


def test_build_tsquery_basic() -> None:
    assert search.build_tsquery("acme ispm") == "acme & ispm:*"
    assert search.build_tsquery("solo") == "solo:*"


def test_build_tsquery_too_short() -> None:
    assert search.build_tsquery("") is None
    assert search.build_tsquery(" a ") is None
    assert search.build_tsquery(None) is None


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


def test_search_finds_feedback(db_session: Session) -> None:
    fid = _make_feedback(db_session, "Zorptastic export button",
                         "please add a zorptastic option to reports")
    groups = search.search(db_session, "zorptastic")
    assert "feedback" in groups
    hit = groups["feedback"][0]
    assert "Zorptastic export button" in hit.title
    assert hit.title.startswith("Feature request · ")  # kind prefix
    assert hit.url == f"/ui/feedback/all/{fid}"
    assert "<mark>" in str(hit.subtitle)


def test_feedback_hidden_from_external(db_session: Session) -> None:
    _make_feedback(db_session, "Grobplex hidden feature", "external must not see this")
    # External viewer: restricted, project-scoped -> feedback (unscoped) is hidden.
    external = search.search(
        db_session, "Grobplex", visible_project_ids=set(), restrict_unscoped=True
    )
    assert "feedback" not in external
    # Internal user merely scoped to "My POCs" still sees all feedback.
    internal = search.search(
        db_session, "Grobplex", visible_project_ids=set(), restrict_unscoped=False
    )
    assert "feedback" in internal


def test_feedback_search_sync(db_session: Session) -> None:
    from app.models import Feedback

    fid = _make_feedback(db_session, "Klaxonate sync check", "alpha")
    assert "feedback" in search.search(db_session, "Klaxonate")

    f = db_session.get(Feedback, fid)
    f.title = "Renamed thlonk item"
    db_session.commit()
    assert search.search(db_session, "Klaxonate") == {}
    assert "feedback" in search.search(db_session, "thlonk")

    db_session.delete(f)
    db_session.commit()
    assert search.search(db_session, "thlonk") == {}


def _add_comment(db: Session, fid: int, body: str) -> int:
    from app.models import FeedbackComment

    c = FeedbackComment(
        feedback_id=fid, author_user_id=None, author_label="Admin", body=body
    )
    db.add(c)
    db.commit()
    return c.id


def test_feedback_comment_text_is_searchable(db_session: Session) -> None:
    fid = _make_feedback(db_session, "Plaindtitle item", "plainbody")
    _add_comment(db_session, fid, "tracking in ENGXYZ ticket")

    groups = search.search(db_session, "ENGXYZ")
    assert "feedback" in groups
    assert groups["feedback"][0].id == fid
    # The matched comment term shows up highlighted in the snippet.
    assert "<mark>ENGXYZ</mark>" in str(groups["feedback"][0].subtitle)


def test_feedback_comment_delete_updates_index(db_session: Session) -> None:
    fid = _make_feedback(db_session, "Plaindtitle two", "plainbody")
    cid = _add_comment(db_session, fid, "term qwixote only in comment")
    assert "feedback" in search.search(db_session, "qwixote")

    from app.models import FeedbackComment

    db_session.delete(db_session.get(FeedbackComment, cid))
    db_session.commit()
    assert search.search(db_session, "qwixote") == {}
    # The item itself is still findable by its own text.
    assert "feedback" in search.search(db_session, "Plaindtitle")


def test_feedback_delete_clears_comment_index(db_session: Session) -> None:
    from app.models import Feedback

    fid = _make_feedback(db_session, "Cascadefeedback item", "body")
    _add_comment(db_session, fid, "zylophone note")
    assert "feedback" in search.search(db_session, "zylophone")

    # Deleting the feedback cascades to its comments; the index is cleaned.
    db_session.delete(db_session.get(Feedback, fid))
    db_session.commit()
    assert search.search(db_session, "zylophone") == {}
    assert search.search(db_session, "Cascadefeedback") == {}


def test_rebuild_index_includes_comment_text(db_session: Session) -> None:
    fid = _make_feedback(db_session, "Rebuildfeedback", "body")
    _add_comment(db_session, fid, "quirkycommentterm")
    assert "feedback" in search.search(db_session, "quirkycommentterm")

    # The maintenance rebuild must fold comments in just like the triggers.
    search.rebuild_index(db_session)
    assert "feedback" in search.search(db_session, "quirkycommentterm")


def test_hyphenated_ticket_id_in_comment_searchable(db_session: Session) -> None:
    # A ticket id that only appears in a comment must match by full id AND by the
    # bare number (separator normalization, migration 0047).
    fid = _make_feedback(db_session, "Ticket linked feature", "see comment")
    _add_comment(db_session, fid, "tracking in ENG-4820 now")
    for q in ("ENG-4820", "4820", "ENG"):
        groups = search.search(db_session, q)
        assert "feedback" in groups, q
        assert any(h.id == fid for h in groups["feedback"]), q


def test_hyphenated_id_searchable_globally(db_session: Session) -> None:
    # Not feedback-specific: a project referencing TICK-991 matches by full id and
    # by the number, proving the normalization applies to every entity.
    pid = _make_project(db_session, "Normalizer POC", "issue ref TICK-991 open")
    for q in ("TICK-991", "991"):
        groups = search.search(db_session, q)
        assert "project" in groups, q
        assert any(h.id == pid for h in groups["project"]), q


def test_rebuild_index_matches_triggers(db_session: Session) -> None:
    _make_project(db_session, "Rebuildable One", "searchable body")
    before = search.search(db_session, "Rebuildable")
    n = search.rebuild_index(db_session)
    assert n >= 1
    after = search.search(db_session, "Rebuildable")
    assert "project" in after
    assert {h.id for h in after["project"]} == {h.id for h in before["project"]}
