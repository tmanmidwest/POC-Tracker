"""normalize hyphenated identifiers in the search index

Postgres' text-search parser indexes a token like ``ENG-482`` as the lexemes
``eng`` and ``-482`` (the number keeps a leading hyphen). Since the query
sanitizer strips punctuation and searches for ``482``, hyphenated ticket ids
never matched — anywhere, on any entity.

Fix at index time: fold ``-`` to a space before ``to_tsvector`` so ``ENG-482`` is
indexed as ``eng`` + ``482``. The query builder already splits ``ENG-482`` into
``eng`` & ``482`` (word tokens), so with no query-side change all of ``ENG-482``,
``482`` and ``ENG`` now match.

This is centralized in a new ``si_tsv(title, body)`` SQL function that every
entity's trigger routes through (installed via ``CREATE OR REPLACE`` on each
trigger *function*, so the triggers themselves are untouched). Existing rows are
re-indexed. ``rebuild_index()`` in app/services/search.py calls ``si_tsv`` too.

Revision ID: 0047_search_normalize_separators
Revises: 0046_index_feedback_comments
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0047_search_normalize_separators"
down_revision: str | Sequence[str] | None = "0046_index_feedback_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = "english"

# The eight column-backed entities (feedback is handled via its reindex fn).
# Mirrors 0012's ENTITIES and app/services/search.py's _INDEX_FIELDS.
ENTITIES: list[dict] = [
    {"type": "project", "table": "projects", "title": ["name"],
     "text": ["name", "notes", "account_executive", "account_executive_email"]},
    {"type": "customer", "table": "customers", "title": ["name"],
     "text": ["name", "notes", "website"]},
    {"type": "contact", "table": "contacts", "title": ["name"],
     "text": ["name", "email", "phone"]},
    {"type": "use_case", "table": "project_use_cases", "title": ["name"],
     "text": ["reference_number", "category", "name", "description",
              "success_validation", "comments"]},
    {"type": "library", "table": "use_case_library", "title": ["name"],
     "text": ["default_reference_number", "category", "name", "description",
              "success_validation"]},
    {"type": "note", "table": "project_notes", "title": ["created_by"],
     "text": ["body"]},
    {"type": "attachment", "table": "note_attachments", "title": ["original_filename"],
     "text": ["original_filename"]},
    {"type": "screenshot", "table": "screenshots", "title": ["caption"],
     "text": ["caption", "original_filename"]},
]


def _expr(cols: list[str], alias: str | None) -> str:
    prefix = f"{alias}." if alias else ""
    return "||' '||".join(f"coalesce({prefix}{c},'')" for c in cols)


def _tsv_inline(title_expr: str, text_expr: str) -> str:
    """The original (un-normalized) tsv construction, for downgrade."""
    return (
        f"setweight(to_tsvector('{_TS}', {title_expr}), 'A') || "
        f"setweight(to_tsvector('{_TS}', {text_expr}), 'B')"
    )


def _entity_fn(etype: str, title_new: str, text_new: str, tsv_sql: str) -> str:
    """Body of a column-backed entity's trigger function (delete-then-insert)."""
    return f"""
        CREATE OR REPLACE FUNCTION si_{etype}_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                DELETE FROM search_index
                 WHERE entity_type='{etype}' AND entity_id=OLD.id;
                RETURN OLD;
            END IF;
            DELETE FROM search_index
             WHERE entity_type='{etype}' AND entity_id=NEW.id;
            INSERT INTO search_index(title, text, entity_type, entity_id, tsv)
            VALUES({title_new}, {text_new}, '{etype}', NEW.id, {tsv_sql});
            RETURN NEW;
        END;
        $$;
    """


def _feedback_reindex_fn(tsv_sql: str) -> str:
    """Body of si_feedback_reindex(fid); ``tsv_sql`` uses v_title/v_text."""
    return f"""
        CREATE OR REPLACE FUNCTION si_feedback_reindex(fid integer) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            v_title text;
            v_text text;
        BEGIN
            SELECT f.title,
                   coalesce(f.title,'') || ' ' || coalesce(f.body,'') || ' ' ||
                   coalesce((SELECT string_agg(c.body, ' ')
                             FROM feedback_comments c
                             WHERE c.feedback_id = f.id), '')
              INTO v_title, v_text
              FROM feedback f
             WHERE f.id = fid;

            DELETE FROM search_index
             WHERE entity_type='feedback' AND entity_id=fid;

            IF v_title IS NOT NULL THEN
                INSERT INTO search_index(title, text, entity_type, entity_id, tsv)
                VALUES(v_title, v_text, 'feedback', fid, {tsv_sql});
            END IF;
        END;
        $$;
    """


def _reindex_all(use_si_tsv: bool) -> None:
    """Rebuild every entity's search rows with the current tsv construction."""
    for e in ENTITIES:
        etype, table = e["type"], e["table"]
        title_row, text_row = _expr(e["title"], None), _expr(e["text"], None)
        tsv = (
            f"si_tsv({title_row}, {text_row})"
            if use_si_tsv
            else _tsv_inline(title_row, text_row)
        )
        op.execute(f"DELETE FROM search_index WHERE entity_type='{etype}'")
        op.execute(
            "INSERT INTO search_index(title, text, entity_type, entity_id, tsv) "
            f"SELECT {title_row}, {text_row}, '{etype}', id, {tsv} FROM {table}"
        )
    op.execute("DELETE FROM search_index WHERE entity_type='feedback'")
    op.execute("SELECT si_feedback_reindex(id) FROM feedback")


def upgrade() -> None:
    # One place that both normalizes separators and applies A/B weighting.
    op.execute(
        f"""
        CREATE FUNCTION si_tsv(a text, b text) RETURNS tsvector
        IMMUTABLE LANGUAGE sql AS $$
            SELECT setweight(to_tsvector('{_TS}', translate(coalesce(a,''), '-', ' ')), 'A') ||
                   setweight(to_tsvector('{_TS}', translate(coalesce(b,''), '-', ' ')), 'B')
        $$;
        """
    )
    for e in ENTITIES:
        title_new, text_new = _expr(e["title"], "NEW"), _expr(e["text"], "NEW")
        op.execute(
            _entity_fn(e["type"], title_new, text_new, f"si_tsv({title_new}, {text_new})")
        )
    op.execute(_feedback_reindex_fn("si_tsv(v_title, v_text)"))
    _reindex_all(use_si_tsv=True)


def downgrade() -> None:
    # Restore inline, un-normalized tsv construction and drop si_tsv.
    for e in ENTITIES:
        title_new, text_new = _expr(e["title"], "NEW"), _expr(e["text"], "NEW")
        op.execute(
            _entity_fn(e["type"], title_new, text_new, _tsv_inline(title_new, text_new))
        )
    op.execute(_feedback_reindex_fn(_tsv_inline("v_title", "v_text")))
    _reindex_all(use_si_tsv=False)
    op.execute("DROP FUNCTION IF EXISTS si_tsv(text, text)")
