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
    op.execute(
        "CREATE VIRTUAL TABLE search_index USING fts5("
        "title, text, entity_type UNINDEXED, entity_id UNINDEXED)"
    )

    for e in ENTITIES:
        etype, table = e["type"], e["table"]
        title_new, text_new = _expr(e["title"], "NEW"), _expr(e["text"], "NEW")
        title_row, text_row = _expr(e["title"], None), _expr(e["text"], None)
        insert_new = (
            "INSERT INTO search_index(title, text, entity_type, entity_id) "
            f"VALUES({title_new}, {text_new}, '{etype}', NEW.id);"
        )
        delete_old = (
            f"DELETE FROM search_index WHERE entity_type='{etype}' "
            "AND entity_id=OLD.id;"
        )

        op.execute(
            f"CREATE TRIGGER si_{etype}_ai AFTER INSERT ON {table} "
            f"BEGIN {insert_new} END"
        )
        op.execute(
            f"CREATE TRIGGER si_{etype}_ad AFTER DELETE ON {table} "
            f"BEGIN {delete_old} END"
        )
        op.execute(
            f"CREATE TRIGGER si_{etype}_au AFTER UPDATE ON {table} "
            f"BEGIN {delete_old} {insert_new} END"
        )

        # Initial populate from existing rows.
        op.execute(
            "INSERT INTO search_index(title, text, entity_type, entity_id) "
            f"SELECT {title_row}, {text_row}, '{etype}', id FROM {table}"
        )


def downgrade() -> None:
    for e in ENTITIES:
        etype = e["type"]
        op.execute(f"DROP TRIGGER IF EXISTS si_{etype}_ai")
        op.execute(f"DROP TRIGGER IF EXISTS si_{etype}_ad")
        op.execute(f"DROP TRIGGER IF EXISTS si_{etype}_au")
    op.execute("DROP TABLE IF EXISTS search_index")
