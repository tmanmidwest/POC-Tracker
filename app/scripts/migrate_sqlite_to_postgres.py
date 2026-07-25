"""One-time data migration: copy a SQLite database into Postgres.

Moves an existing SQLite instance's data into a Postgres database as part of the
SQLite -> Postgres cutover (see docs/postgres-migration-plan.md, Phase 5).

Prerequisites:
  1. The **target Postgres** database exists and is at the SAME Alembic revision
     as the source. Create it schema-only first:
         POCT_DATABASE_URL='postgresql+psycopg://user:pw@host/db' \\
             .venv/bin/alembic upgrade head
     (Run migrations only — do NOT boot the app, so no seed rows are written.)
  2. The **source SQLite** file is a consistent copy (stop the app, or restore a
     backup archive's db/poct.db).

Usage:
    .venv/bin/python -m app.scripts.migrate_sqlite_to_postgres \\
        --source /data/poct.db \\
        --target 'postgresql+psycopg://user:pw@host:5432/db'
    # add --yes to skip the confirmation prompt.

What it does (inside one transaction on the target):
  * TRUNCATEs every table (incl. the search index), removing any migration-seeded
    rows so the result is an exact copy — sequences are reset afterwards.
  * Copies every table in FK-dependency order, preserving primary keys.
  * Lets the search-index triggers repopulate as rows are inserted, then resets
    Postgres sequences to each table's max id.

Idempotent to re-run: it truncates the target first, so a second run reloads
cleanly. It refuses to run unless the target is a Postgres DB at the source's
revision.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url

# Importing the models package registers every table on Base.metadata.
import app.models  # noqa: F401
from app.db import Base
from app.logging_config import configure_logging

log = logging.getLogger(__name__)

# Not an ORM model (created by raw SQL in migration 0012); truncated explicitly
# and repopulated by the per-table triggers as rows are inserted.
_SEARCH_INDEX = "search_index"


def _revision(engine: Engine) -> str | None:
    try:
        with engine.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None


def _copy_table(src: Engine, dst_conn, table) -> int:
    """Copy all rows of one table from source to target. Returns row count."""
    with src.connect() as sc:
        rows = [dict(m) for m in sc.execute(select(table)).mappings()]
    if rows:
        dst_conn.execute(table.insert(), rows)
    return len(rows)


def _reset_sequence(dst_conn, table) -> None:
    """Reset a Postgres serial sequence to the table's current max id."""
    pk_cols = list(table.primary_key.columns)
    if len(pk_cols) != 1:
        return  # composite PKs aren't serial
    col = pk_cols[0].name
    seq = dst_conn.execute(
        text("SELECT pg_get_serial_sequence(:t, :c)"),
        {"t": table.name, "c": col},
    ).scalar()
    if not seq:
        return  # no sequence (fixed-id singleton, string PK, etc.)
    dst_conn.execute(
        text(
            f'SELECT setval(:seq, COALESCE((SELECT MAX("{col}") FROM "{table.name}"), 1), '
            f'(SELECT MAX("{col}") FROM "{table.name}") IS NOT NULL)'
        ),
        {"seq": seq},
    )


def migrate(source_sqlite: Path, target_url: str, *, assume_yes: bool) -> int:
    src_url = f"sqlite:///{source_sqlite}"
    if make_url(target_url).get_backend_name() != "postgresql":
        print(f"[ERROR] --target must be a Postgres URL, got: {target_url}")
        return 2
    if not source_sqlite.exists():
        print(f"[ERROR] source SQLite file not found: {source_sqlite}")
        return 2

    src = create_engine(src_url)
    dst = create_engine(target_url)

    src_rev, dst_rev = _revision(src), _revision(dst)
    if src_rev is None:
        print(f"[ERROR] source has no alembic_version — not a Questlog DB? {source_sqlite}")
        return 2
    if dst_rev != src_rev:
        print(
            "[ERROR] target schema revision does not match the source.\n"
            f"        source: {src_rev}\n        target: {dst_rev}\n"
            "        Run `alembic upgrade head` against the target first "
            "(migrations only, no app boot)."
        )
        return 2

    tables = list(Base.metadata.sorted_tables)  # FK-dependency order (parents first)
    target = make_url(target_url)
    print(
        f"\nAbout to migrate data:\n"
        f"  source (SQLite): {source_sqlite}\n"
        f"  target (Postgres): {target.host}:{target.port or 5432}/{target.database}\n"
        f"  revision: {src_rev}\n"
        f"  This TRUNCATEs all {len(tables)} target tables (+ search index) first.\n"
    )
    if not assume_yes:
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    counts: dict[str, int] = {}
    with dst.begin() as dc:
        # Clear any migration-seeded rows so the copy is exact. CASCADE handles FK
        # order; RESTART IDENTITY zeroes sequences (reset to real max below).
        all_names = ", ".join(f'"{t.name}"' for t in tables) + f', "{_SEARCH_INDEX}"'
        dc.execute(text(f"TRUNCATE {all_names} RESTART IDENTITY CASCADE"))

        for table in tables:
            counts[table.name] = _copy_table(src, dc, table)
        for table in tables:
            _reset_sequence(dc, table)

    total = sum(counts.values())
    print("\nCopied rows:")
    for name, n in sorted(counts.items()):
        if n:
            print(f"  {name:32s} {n}")
    idx = 0
    with dst.connect() as c:
        idx = c.execute(text(f'SELECT count(*) FROM "{_SEARCH_INDEX}"')).scalar() or 0
    print(f"\n[OK] Migrated {total} rows across {len(tables)} tables; "
          f"search index rebuilt with {idx} entries.")
    log.info("sqlite_to_postgres_migrated", extra={"rows": total, "tables": len(tables)})
    return 0


def main() -> int:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="Copy a SQLite DB into Postgres.")
    parser.add_argument(
        "--source", required=True, type=Path,
        help="Path to the source SQLite file (e.g. /data/poct.db).",
    )
    parser.add_argument(
        "--target", required=True,
        help="Target Postgres SQLAlchemy URL (postgresql+psycopg://…).",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt.",
    )
    args = parser.parse_args()
    return migrate(args.source, args.target, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
