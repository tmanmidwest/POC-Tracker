"""index feedback in the global search

Wires the ``feedback`` table into the unified ``search_index`` (added in 0012) so
bug reports and feature requests show up in the top-bar search. Same pattern as
every other entity: one delete-then-insert plpgsql trigger keeping the row
current, plus an initial backfill. Title = the feedback title (weight A); body =
title + details (weight B).

Comments (``feedback_comments``) are intentionally not indexed here — only the
submission's own title/body.

Revision ID: 0045_add_feedback_to_search
Revises: 0044_add_feedback_comments
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0045_add_feedback_to_search"
down_revision: str | Sequence[str] | None = "0044_add_feedback_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS_CONFIG = "english"
_ETYPE = "feedback"
_TABLE = "feedback"
_TITLE_COLS = ["title"]
_TEXT_COLS = ["title", "body"]


def _expr(cols: list[str], alias: str | None) -> str:
    prefix = f"{alias}." if alias else ""
    return "||' '||".join(f"coalesce({prefix}{c},'')" for c in cols)


def _tsv(title_expr: str, text_expr: str) -> str:
    return (
        f"setweight(to_tsvector('{_TS_CONFIG}', {title_expr}), 'A') || "
        f"setweight(to_tsvector('{_TS_CONFIG}', {text_expr}), 'B')"
    )


def upgrade() -> None:
    title_new, text_new = _expr(_TITLE_COLS, "NEW"), _expr(_TEXT_COLS, "NEW")
    title_row, text_row = _expr(_TITLE_COLS, None), _expr(_TEXT_COLS, None)

    op.execute(
        f"""
        CREATE FUNCTION si_{_ETYPE}_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                DELETE FROM search_index
                 WHERE entity_type='{_ETYPE}' AND entity_id=OLD.id;
                RETURN OLD;
            END IF;
            DELETE FROM search_index
             WHERE entity_type='{_ETYPE}' AND entity_id=NEW.id;
            INSERT INTO search_index(title, text, entity_type, entity_id, tsv)
            VALUES({title_new}, {text_new}, '{_ETYPE}', NEW.id,
                   {_tsv(title_new, text_new)});
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        f"CREATE TRIGGER si_{_ETYPE}_aiud "
        f"AFTER INSERT OR UPDATE OR DELETE ON {_TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION si_{_ETYPE}_fn()"
    )
    op.execute(
        "INSERT INTO search_index(title, text, entity_type, entity_id, tsv) "
        f"SELECT {title_row}, {text_row}, '{_ETYPE}', id, "
        f"{_tsv(title_row, text_row)} FROM {_TABLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS si_{_ETYPE}_aiud ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS si_{_ETYPE}_fn()")
    op.execute(f"DELETE FROM search_index WHERE entity_type='{_ETYPE}'")
