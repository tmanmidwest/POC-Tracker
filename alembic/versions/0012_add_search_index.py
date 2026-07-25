"""add FTS5 full-text search index

Creates a single standalone FTS5 virtual table (``search_index``) that indexes
the searchable text of every domain entity, plus per-table triggers that keep it
in sync on insert/update/delete. Notes index the *plain-text* body/notes columns
(not the rich-text HTML). Display titles/links are resolved in Python at query
time, so triggers only ever read their own table's columns.

Revision ID: 0012_search_index
Revises: 0011_backup_runs
Create Date: 2026-06-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_search_index"
down_revision: str | Sequence[str] | None = "0011_backup_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Per-entity index config: which columns feed the (weighted) title and the
# full searchable text. Keep in sync with app/services/search.py.
ENTITIES: list[dict] = [
    {
        "type": "project",
        "table": "projects",
        "title": ["name"],
        "text": ["name", "notes", "account_executive", "account_executive_email"],
    },
    {
        "type": "customer",
        "table": "customers",
        "title": ["name"],
        "text": ["name", "notes", "website"],
    },
    {
        "type": "contact",
        "table": "contacts",
        "title": ["name"],
        "text": ["name", "email", "phone"],
    },
    {
        "type": "use_case",
        "table": "project_use_cases",
        "title": ["name"],
        "text": ["reference_number", "category", "name", "description",
                 "success_validation", "comments"],
    },
    {
        "type": "library",
        "table": "use_case_library",
        "title": ["name"],
        "text": ["default_reference_number", "category", "name", "description",
                 "success_validation"],
    },
    {
        "type": "note",
        "table": "project_notes",
        "title": ["created_by"],
        "text": ["body"],
    },
    {
        "type": "attachment",
        "table": "note_attachments",
        "title": ["original_filename"],
        "text": ["original_filename"],
    },
    {
        "type": "screenshot",
        "table": "screenshots",
        "title": ["caption"],
        "text": ["caption", "original_filename"],
    },
]


def _expr(cols: list[str], alias: str | None) -> str:
    """SQL expression concatenating columns into one searchable string."""
    prefix = f"{alias}." if alias else ""
    return "||' '||".join(f"coalesce({prefix}{c},'')" for c in cols)


def upgrade() -> None:
    _upgrade_postgres()


def downgrade() -> None:
    _downgrade_postgres()


# --- Postgres: real table + tsvector/GIN + per-entity plpgsql triggers ----------
#
# Mirrors the SQLite design: one unified ``search_index`` row per entity, kept in
# sync by triggers that only ever read their own table's columns. Ranking weight
# (title above body) is carried by tsvector weights A/B here instead of the FTS5
# bm25 column weights used on SQLite. The query side (app/services/search.py) gets
# its Postgres branch in the search rewrite (migration plan Phase 2).

_TS_CONFIG = "english"


def _tsv(title_expr: str, text_expr: str) -> str:
    """Weighted tsvector: title -> weight A, body -> weight B."""
    return (
        f"setweight(to_tsvector('{_TS_CONFIG}', {title_expr}), 'A') || "
        f"setweight(to_tsvector('{_TS_CONFIG}', {text_expr}), 'B')"
    )


def _upgrade_postgres() -> None:
    op.execute(
        "CREATE TABLE search_index ("
        " id BIGSERIAL PRIMARY KEY,"
        " title TEXT NOT NULL DEFAULT '',"
        " text TEXT NOT NULL DEFAULT '',"
        " entity_type TEXT NOT NULL,"
        " entity_id INTEGER NOT NULL,"
        " tsv tsvector)"
    )
    # One row per entity (also speeds the trigger's delete-then-insert).
    op.execute(
        "CREATE UNIQUE INDEX ux_search_index_entity "
        "ON search_index(entity_type, entity_id)"
    )
    op.execute("CREATE INDEX ix_search_index_tsv ON search_index USING GIN(tsv)")

    for e in ENTITIES:
        etype, table = e["type"], e["table"]
        title_new, text_new = _expr(e["title"], "NEW"), _expr(e["text"], "NEW")
        title_row, text_row = _expr(e["title"], None), _expr(e["text"], None)

        # A single AFTER INSERT/UPDATE/DELETE function per entity. UPDATE and
        # INSERT both delete-then-insert so the row is always current.
        op.execute(
            f"""
            CREATE FUNCTION si_{etype}_fn() RETURNS trigger
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
                VALUES({title_new}, {text_new}, '{etype}', NEW.id,
                       {_tsv(title_new, text_new)});
                RETURN NEW;
            END;
            $$;
            """
        )
        op.execute(
            f"CREATE TRIGGER si_{etype}_aiud "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION si_{etype}_fn()"
        )

        # Initial populate from existing rows.
        op.execute(
            "INSERT INTO search_index(title, text, entity_type, entity_id, tsv) "
            f"SELECT {title_row}, {text_row}, '{etype}', id, "
            f"{_tsv(title_row, text_row)} FROM {table}"
        )


def _downgrade_postgres() -> None:
    for e in ENTITIES:
        etype, table = e["type"], e["table"]
        op.execute(f"DROP TRIGGER IF EXISTS si_{etype}_aiud ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS si_{etype}_fn()")
    op.execute("DROP TABLE IF EXISTS search_index")
