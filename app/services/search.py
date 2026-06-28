"""Global full-text search over all domain entities.

Queries the unified ``search_index`` FTS5 table (built and kept in sync by the
0012 migration's triggers), ranks with bm25, then resolves the matched
``(entity_type, entity_id)`` pairs back to real objects to build display titles,
links, and highlighted snippets.

Safety:
* User input never reaches FTS5 ``MATCH`` raw — :func:`build_match_query`
  rebuilds it from extracted word tokens (quoted phrases + a trailing prefix),
  so stray quotes/operators can't cause a syntax error.
* Results are bounded (overall cap + per-type limit) so a broad query can't
  return an unbounded set.
* Snippets are HTML-escaped before ``<mark>`` highlighting, so indexed user text
  can't inject markup.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from html import escape

from markupsafe import Markup
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models import (
    Contact,
    Customer,
    NoteAttachment,
    Project,
    ProjectNote,
    ProjectUseCase,
    Screenshot,
    UseCaseLibrary,
)

MIN_QUERY_LEN = 2
DEFAULT_PER_TYPE = 8
OVERALL_CAP = 60
_MAX_TOKENS = 10
_SNIPPET_WINDOW = 140

_WORD_RE = re.compile(r"\w+", re.UNICODE)

TYPE_LABELS = {
    "project": "Projects",
    "use_case": "Use cases",
    "library": "Use case library",
    "note": "Notes",
    "customer": "Customers",
    "contact": "Contacts",
    "attachment": "Attachments",
    "screenshot": "Screenshots",
}
# Display order of result groups.
TYPE_ORDER = [
    "project", "use_case", "library", "note",
    "customer", "contact", "attachment", "screenshot",
]

_MODELS = {
    "project": Project,
    "customer": Customer,
    "contact": Contact,
    "use_case": ProjectUseCase,
    "library": UseCaseLibrary,
    "note": ProjectNote,
    "attachment": NoteAttachment,
    "screenshot": Screenshot,
}

# Columns fed into the index title/text per entity — mirrors the 0012 migration
# triggers. Used by rebuild_index() so it stays consistent with the triggers.
_INDEX_FIELDS: dict[str, tuple[list[str], list[str]]] = {
    "project": (["name"], ["name", "notes", "account_executive", "account_executive_email"]),
    "customer": (["name"], ["name", "notes", "website"]),
    "contact": (["name"], ["name", "email", "phone"]),
    "use_case": (["name"], ["reference_number", "category", "name", "description",
                            "success_validation", "comments"]),
    "library": (["name"], ["default_reference_number", "category", "name", "description",
                           "success_validation"]),
    "note": (["created_by"], ["body"]),
    "attachment": (["original_filename"], ["original_filename"]),
    "screenshot": (["caption"], ["caption", "original_filename"]),
}


@dataclass
class SearchHit:
    """One resolved search result, ready to render."""

    type: str
    type_label: str
    id: int
    title: str
    subtitle: Markup | None
    url: str
    score: float


def _tokens(raw: str | None) -> list[str]:
    """Extract word tokens from raw input (drops quotes/operators/punctuation)."""
    return _WORD_RE.findall(raw or "")[:_MAX_TOKENS]


def build_match_query(raw: str | None) -> str | None:
    """Turn raw user input into a safe FTS5 MATCH expression, or None if too short.

    Each token becomes a quoted phrase (AND-ed together); the last token gets a
    ``*`` for prefix matching, which powers as-you-type search.
    """
    if not raw or len(raw.strip()) < MIN_QUERY_LEN:
        return None
    toks = _tokens(raw)
    if not toks:
        return None
    parts = [f'"{t}"' for t in toks[:-1]] + [f'"{toks[-1]}"*']
    return " ".join(parts)


def _highlight(value: str | None, tokens: list[str]) -> Markup | None:
    """Build an HTML-escaped, <mark>-highlighted snippet windowed on the match."""
    if not value:
        return None
    plain = " ".join(value.split())
    low = plain.lower()
    pos = -1
    for t in tokens:
        i = low.find(t.lower())
        if i != -1 and (pos == -1 or i < pos):
            pos = i

    if pos <= 40:
        start, lead = 0, ""
    else:
        start, lead = pos - 40, "…"
    window = plain[start:start + _SNIPPET_WINDOW]
    trail = "…" if start + _SNIPPET_WINDOW < len(plain) else ""

    esc = escape(window)
    if tokens:
        pattern = re.compile("|".join(re.escape(t) for t in set(tokens)), re.IGNORECASE)
        esc = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", esc)
    return Markup(lead + esc + trail)


# --- Per-type display builders: (object) -> (title, snippet_source_text, url) ---


def _b_project(p: Project) -> tuple[str, str, str]:
    text = " ".join(filter(None, [p.name, p.notes, p.account_executive]))
    return p.display_name, text, f"/ui/projects/{p.id}"


def _b_customer(c: Customer) -> tuple[str, str, str]:
    return c.name, " ".join(filter(None, [c.name, c.notes, c.website])), f"/ui/customers/{c.id}"


def _b_contact(c: Contact) -> tuple[str, str, str]:
    return c.name, " ".join(filter(None, [c.name, c.email, c.phone])), f"/ui/customers/{c.customer_id}"


def _b_use_case(u: ProjectUseCase) -> tuple[str, str, str]:
    text = " ".join(filter(None, [u.reference_number, u.category, u.name, u.description,
                                  u.success_validation, u.comments]))
    return u.name, text, f"/ui/projects/{u.project_id}#use-cases"


def _b_library(e: UseCaseLibrary) -> tuple[str, str, str]:
    text = " ".join(filter(None, [e.default_reference_number, e.category, e.name, e.description]))
    return e.name, text, "/ui/library"


def _b_note(n: ProjectNote) -> tuple[str, str, str]:
    return f"Note · {n.note_date.strftime('%b %-d, %Y')}", n.body, f"/ui/projects/{n.project_id}#notes"


def _b_attachment(a: NoteAttachment) -> tuple[str, str, str]:
    pid = a.note.project_id if a.note else None
    url = f"/ui/projects/{pid}#notes" if pid else "#"
    return a.original_filename or a.stored_filename, a.original_filename or "", url


def _b_screenshot(s: Screenshot) -> tuple[str, str, str]:
    pid = s.use_case.project_id if s.use_case else None
    url = f"/ui/projects/{pid}#use-cases" if pid else "#"
    return s.caption or s.original_filename or s.stored_filename, \
        " ".join(filter(None, [s.caption, s.original_filename])), url


_BUILDERS = {
    "project": _b_project,
    "customer": _b_customer,
    "contact": _b_contact,
    "use_case": _b_use_case,
    "library": _b_library,
    "note": _b_note,
    "attachment": _b_attachment,
    "screenshot": _b_screenshot,
}


# Entity types scoped to a single project, and how to find that project's id.
# Types not listed here (customer, contact, library) are not project-scoped and
# are hidden entirely from external viewers.
_PROJECT_ID_OF = {
    "project": lambda o: o.id,
    "use_case": lambda o: o.project_id,
    "note": lambda o: o.project_id,
    "attachment": lambda o: o.note.project_id if o.note else None,
    "screenshot": lambda o: o.use_case.project_id if o.use_case else None,
}


def _visible_to(
    etype: str,
    obj: object,
    visible_project_ids: set[int] | None,
    restrict_unscoped: bool = True,
) -> bool:
    """Whether a hit may be shown given the caller's accessible projects.

    ``visible_project_ids`` is None for "search everything" (no project filter).
    When it's a set, project-scoped hits are limited to those ids.

    ``restrict_unscoped`` controls non-project-scoped types (customers, library,
    contacts). True for external viewers — they see nothing outside their granted
    projects. False for an internal user merely scoped to "My POCs" — they still
    see global reference data, just narrowed project content.
    """
    if visible_project_ids is None:
        return True
    resolver = _PROJECT_ID_OF.get(etype)
    if resolver is None:
        return not restrict_unscoped
    return resolver(obj) in visible_project_ids


def search(
    db: Session,
    raw: str | None,
    *,
    per_type_limit: int = DEFAULT_PER_TYPE,
    overall_cap: int = OVERALL_CAP,
    visible_project_ids: set[int] | None = None,
    restrict_unscoped: bool = True,
) -> dict[str, list[SearchHit]]:
    """Run a bounded, ranked full-text search; return hits grouped by entity type.

    Pass ``visible_project_ids`` to scope project content to a set of ids; leave
    it None to search every project. ``restrict_unscoped`` hides non-project
    types (customers, library) — keep it True for external viewers, set it False
    for an internal user merely scoped to "My POCs".
    """
    match = build_match_query(raw)
    if not match:
        return {}

    rows = db.execute(
        sql_text(
            "SELECT entity_type, entity_id, bm25(search_index, 3.0, 1.0) AS score "
            "FROM search_index WHERE search_index MATCH :q "
            "ORDER BY score LIMIT :lim"
        ),
        {"q": match, "lim": overall_cap},
    ).all()
    if not rows:
        return {}

    ids_by_type: dict[str, list[int]] = defaultdict(list)
    for etype, eid, _score in rows:
        ids_by_type[etype].append(eid)

    # Batch-load each type's matched rows.
    loaded: dict[tuple[str, int], object] = {}
    for etype, ids in ids_by_type.items():
        model = _MODELS.get(etype)
        if model is None:
            continue
        for obj in db.query(model).filter(model.id.in_(ids)).all():
            loaded[(etype, obj.id)] = obj

    tokens = _tokens(raw)
    grouped: dict[str, list[SearchHit]] = {}
    for etype, eid, score in rows:
        obj = loaded.get((etype, eid))
        if obj is None:
            continue  # stale index row (e.g. cascade-deleted) — skip
        if not _visible_to(etype, obj, visible_project_ids, restrict_unscoped):
            continue  # outside the caller's accessible / scoped projects
        bucket = grouped.setdefault(etype, [])
        if len(bucket) >= per_type_limit:
            continue
        title, text, url = _BUILDERS[etype](obj)
        bucket.append(
            SearchHit(etype, TYPE_LABELS[etype], eid, title, _highlight(text, tokens), url, score)
        )

    return {t: grouped[t] for t in TYPE_ORDER if t in grouped}


def total_hits(grouped: dict[str, list[SearchHit]]) -> int:
    return sum(len(v) for v in grouped.values())


def rebuild_index(db: Session) -> int:
    """Wipe and repopulate the search index from current rows. A maintenance/
    safety backstop — normal operation is kept current by triggers. Returns the
    number of indexed rows."""
    db.execute(sql_text("DELETE FROM search_index"))
    count = 0
    for etype, model in _MODELS.items():
        title_cols, text_cols = _INDEX_FIELDS[etype]
        for obj in db.query(model).all():
            title = " ".join(str(getattr(obj, c)) for c in title_cols if getattr(obj, c, None))
            body = " ".join(str(getattr(obj, c)) for c in text_cols if getattr(obj, c, None))
            db.execute(
                sql_text(
                    "INSERT INTO search_index(title, text, entity_type, entity_id) "
                    "VALUES(:t, :x, :et, :id)"
                ),
                {"t": title, "x": body, "et": etype, "id": obj.id},
            )
            count += 1
    db.commit()
    return count
