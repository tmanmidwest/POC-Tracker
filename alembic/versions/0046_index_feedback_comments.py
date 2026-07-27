"""fold feedback comments into the search index

0045 indexed a feedback item's own title/body. This extends it so the admin
comment thread is searchable too (e.g. a ticket id that only appears in a
comment). A feedback item's ``search_index`` row now also includes the
concatenated text of its comments (weight B).

A single ``si_feedback_reindex(fid)`` function rebuilds one feedback row from the
item + all its comments, and is the shared source of truth for two triggers:
  * ``si_feedback_aiud`` on ``feedback`` (replacing 0045's), and
  * ``si_feedback_comment_aiud`` on ``feedback_comments`` (new) — so adding,
    editing, or removing a comment reindexes its parent.

Revision ID: 0046_index_feedback_comments
Revises: 0045_add_feedback_to_search
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0046_index_feedback_comments"
down_revision: str | Sequence[str] | None = "0045_add_feedback_to_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Replace 0045's feedback trigger with one built on the shared reindex fn.
    op.execute("DROP TRIGGER IF EXISTS si_feedback_aiud ON feedback")
    op.execute("DROP FUNCTION IF EXISTS si_feedback_fn()")

    # Rebuild one feedback row from the item + all its comments. Called by both
    # triggers below. NULL v_title (feedback row gone) => just clear the index row.
    op.execute(
        """
        CREATE FUNCTION si_feedback_reindex(fid integer) RETURNS void
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
                VALUES(v_title, v_text, 'feedback', fid,
                       setweight(to_tsvector('english', coalesce(v_title,'')), 'A') ||
                       setweight(to_tsvector('english', coalesce(v_text,'')), 'B'));
            END IF;
        END;
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION si_feedback_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                DELETE FROM search_index
                 WHERE entity_type='feedback' AND entity_id=OLD.id;
                RETURN OLD;
            END IF;
            PERFORM si_feedback_reindex(NEW.id);
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER si_feedback_aiud "
        "AFTER INSERT OR UPDATE OR DELETE ON feedback "
        "FOR EACH ROW EXECUTE FUNCTION si_feedback_fn()"
    )

    op.execute(
        """
        CREATE FUNCTION si_feedback_comment_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                PERFORM si_feedback_reindex(OLD.feedback_id);
                RETURN OLD;
            END IF;
            IF (TG_OP = 'UPDATE' AND OLD.feedback_id <> NEW.feedback_id) THEN
                PERFORM si_feedback_reindex(OLD.feedback_id);
            END IF;
            PERFORM si_feedback_reindex(NEW.feedback_id);
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER si_feedback_comment_aiud "
        "AFTER INSERT OR UPDATE OR DELETE ON feedback_comments "
        "FOR EACH ROW EXECUTE FUNCTION si_feedback_comment_fn()"
    )

    # Rebuild existing feedback rows so comment text is folded in now.
    op.execute("SELECT si_feedback_reindex(id) FROM feedback")


def downgrade() -> None:
    # Restore the 0045 state: feedback indexed on its own title+body only.
    op.execute("DROP TRIGGER IF EXISTS si_feedback_comment_aiud ON feedback_comments")
    op.execute("DROP FUNCTION IF EXISTS si_feedback_comment_fn()")
    op.execute("DROP TRIGGER IF EXISTS si_feedback_aiud ON feedback")
    op.execute("DROP FUNCTION IF EXISTS si_feedback_fn()")
    op.execute("DROP FUNCTION IF EXISTS si_feedback_reindex(integer)")

    op.execute(
        """
        CREATE FUNCTION si_feedback_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                DELETE FROM search_index
                 WHERE entity_type='feedback' AND entity_id=OLD.id;
                RETURN OLD;
            END IF;
            DELETE FROM search_index
             WHERE entity_type='feedback' AND entity_id=NEW.id;
            INSERT INTO search_index(title, text, entity_type, entity_id, tsv)
            VALUES(coalesce(NEW.title,''),
                   coalesce(NEW.title,'')||' '||coalesce(NEW.body,''),
                   'feedback', NEW.id,
                   setweight(to_tsvector('english', coalesce(NEW.title,'')), 'A') ||
                   setweight(to_tsvector('english',
                       coalesce(NEW.title,'')||' '||coalesce(NEW.body,'')), 'B'));
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER si_feedback_aiud "
        "AFTER INSERT OR UPDATE OR DELETE ON feedback "
        "FOR EACH ROW EXECUTE FUNCTION si_feedback_fn()"
    )
    op.execute("DELETE FROM search_index WHERE entity_type='feedback'")
    op.execute(
        "INSERT INTO search_index(title, text, entity_type, entity_id, tsv) "
        "SELECT coalesce(title,''), coalesce(title,'')||' '||coalesce(body,''), "
        "'feedback', id, "
        "setweight(to_tsvector('english', coalesce(title,'')), 'A') || "
        "setweight(to_tsvector('english', "
        "coalesce(title,'')||' '||coalesce(body,'')), 'B') "
        "FROM feedback"
    )
