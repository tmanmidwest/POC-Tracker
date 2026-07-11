# Postgres migration — planning notes

> **Status: not built. Planning only.** The app runs on SQLite today and that's
> fine for an internal SE-team tool (see [Why / when](#why--when)). This document
> captures what a move to Postgres would involve so it's ready if/when the team
> outgrows a single instance. Nothing here is implemented.

- [Why / when](#why--when)
- [The mental model](#the-mental-model)
- [The easy part (connection + engine)](#the-easy-part-connection--engine)
- [The hard part (SQLite-specific subsystems)](#the-hard-part-sqlite-specific-subsystems)
- [Scaling out is a *separate* project](#scaling-out-is-a-separate-project)
- [Recommendation: dual-DB, SQLite stays default](#recommendation-dual-db-sqlite-stays-default)
- [Phased build plan](#phased-build-plan)
- [Effort estimate](#effort-estimate)

---

## Why / when

SQLite is configured well here — WAL mode, `busy_timeout`, sane pragmas
([db.py](../app/db.py)) — and the workload is read-heavy, so a normal SE team
(dozens of concurrent users) runs comfortably. The practical limits that would
trigger this migration:

- Sustained **write** contention (many people editing at once) hitting SQLite's
  single-writer serialization.
- Wanting to run **multiple app containers** behind a load balancer (SQLite is a
  local file — you can't safely share it across hosts).
- Wanting managed-DB **HA / point-in-time recovery / snapshots** (e.g. AWS RDS)
  instead of the built-in file backup.

Until one of those bites, staying on SQLite is the right call.

## The mental model

Everything goes through **SQLAlchemy + Alembic**, so the generic "talk to a
different database" part is small. The real work is in a few places that lean on
SQLite-specific behavior, plus the operational changes to run more than one app
instance. Roughly **20% connection swap, 80% subsystems + ops**.

## The easy part (connection + engine)

| Change | Where | Notes |
|---|---|---|
| Make the DB URL configurable | [config.py](../app/config.py) `database_url` | Today it hardcodes `sqlite:///<data_dir>/poct.db`. Add `POCT_DATABASE_URL`, default to the SQLite path. |
| Branch the engine setup by dialect | [db.py](../app/db.py) `_build_engine` | The `PRAGMA journal_mode=WAL / synchronous / busy_timeout / recursive_triggers` and `check_same_thread` args are **SQLite-only**. Apply them only when `dialect == "sqlite"`; add a Postgres branch with `pool_pre_ping`, `pool_size`, etc. |
| Add the driver + a DB service | `pyproject.toml`, `docker-compose.yml` | `psycopg` (v3), a `postgres` compose service, and a health/readiness wait on boot. |

After this the app can *boot* against an empty Postgres. The subsystems below are
what make it actually work.

## The hard part (SQLite-specific subsystems)

### 1. Full-text search — the biggest piece

Search is built on **SQLite FTS5**: [migration 0012](../alembic/versions/0012_add_search_index.py)
creates a `search_index` virtual table with `MATCH` + `bm25()` ranking, kept in
sync by DB triggers, and [search.py](../app/services/search.py) queries it
directly (`… search_index MATCH :q …`, `bm25(search_index, …)`). None of FTS5,
`MATCH`, or `bm25()` exists in Postgres.

**Rework:** a Postgres search path using `tsvector` + a GIN index +
`plainto_tsquery`/`to_tsquery` + `ts_rank` (or `pg_trgm` for fuzzy matching). The
sync triggers become Postgres triggers (or a `tsvector` generated column), and the
query builder in `search.py` gets a dialect branch behind the same `search()`
interface. **This is the single largest item.**

### 2. Backups

[backups.py](../app/services/backups.py) is built around SQLite being one file: it
uses `sqlite3.Connection.backup()` for a consistent snapshot, zips it with the
uploaded files + keys into a downloadable AES archive, and restore swaps the file
back in on startup ([apply_pending_restore] in [main.py](../app/main.py) lifespan).

**Rework:** with Postgres there's no file to copy. DB backup/restore moves to
`pg_dump`/`pg_restore`, or (with managed Postgres) automated snapshots. Options:
(a) a Postgres-aware backup path, or (b) scope the in-app archive to
**files + keys only** and hand DB backups to Postgres tooling. The startup
file-swap restore no longer applies to the DB.

### 3. Dialect-aware migrations

- [env.py](../alembic/env.py) sets `render_as_batch=True` — a SQLite `ALTER TABLE`
  workaround Postgres doesn't want. Make it conditional (SQLite only).
- Some migrations contain raw SQLite SQL: the FTS setup (0012 and any FTS-touching
  ones), and note that [0026](../alembic/versions/0026_add_external_user_expiry.py)
  uses `WHERE is_external = 1`, which **errors on Postgres** (boolean ≠ integer).
  Migrations with raw SQL / FTS must branch on `op.get_bind().dialect.name` so the
  same history runs on both databases.

### 4. Type-strictness / timezone audit

SQLite is loose; Postgres is strict. Naive vs. timezone-aware datetimes (SQLite
drops tzinfo — hence the `.replace(tzinfo=UTC)` coercions dotted around the code),
booleans as `0/1` vs real `true/false`, etc. The ORM shields most of this, but any
raw SQL and datetime comparisons need a pass. Add a CI job that runs the **full
test suite against Postgres too**, so the two can't silently drift.

## Scaling out is a *separate* project

**Postgres removes the single-writer limit, but by itself it does NOT let you run
multiple app containers.** For true horizontal scale you also need:

- **Shared file storage.** Screenshots, note attachments, the deck template/logo,
  and backups all live on the local `data_dir` volume. With two containers,
  container B can't serve a file uploaded through container A. Uploads must move to
  shared storage (S3/EFS). *As much work as the DB swap, and easy to forget.*
- **Single-runner background jobs.** The daily sweeps (`_audit_retention_loop`,
  `_external_expiry_loop` in [main.py](../app/main.py)) run in-process — N
  containers would run them N times (duplicate expiry emails, etc.). Guard with a
  Postgres advisory lock so only one instance runs them, or move to a dedicated
  worker.
- **Migration coordination.** Migrations run on startup; N containers booting would
  race. Wrap in an advisory lock, or run migrations as a separate deploy step.
- **Cache coherence.** In-process caches (branding, system settings) are
  per-container — a change on one wouldn't invalidate the others. Fix with a short
  TTL or Postgres `LISTEN/NOTIFY`; for rarely-changed settings, often acceptable
  as-is.

You can do the Postgres swap **without** this section (bigger single instance, no
single-writer limit, managed-DB HA/backups) and defer horizontal scale until truly
needed.

## Recommendation: dual-DB, SQLite stays default

**Add Postgres as an opt-in; keep SQLite as the default.** Don't remove SQLite —
it's the zero-dependency mode for demos and small instances. Make the code
dialect-aware so the *same* codebase runs either way based on `POCT_DATABASE_URL`.
That delivers the capacity "even though not needed yet" without losing the
lightweight setup, and the both-DBs CI matrix keeps them honest.

## Phased build plan

1. **Foundation** — configurable `POCT_DATABASE_URL`, dialect-branched
   engine/pragmas, add `psycopg` + a compose Postgres service; app boots against
   empty Postgres. *Fully backward-compatible with today's SQLite setup.*
2. **Migrations** — make FTS migrations and raw SQL dialect-aware; verify a clean
   `alembic upgrade head` on Postgres.
3. **Search rewrite** — Postgres `tsvector` search path behind the existing
   `search()` interface. Biggest single item.
4. **Backups** — Postgres backup/restore path (or scope to managed-DB snapshots +
   a files/keys archive).
5. **Type/tz audit + CI matrix** — run the full test suite against **both**
   databases so they can't drift.
6. **(Only when scaling out)** shared object storage, single-runner jobs, migration
   locking, cache strategy.

## Effort estimate

- **Postgres-*capable* (steps 1–5):** ~2 weeks of focused work. Search (step 3) and
  backups (step 4) dominate.
- **Horizontal-scale hardening (step 6):** a separate chunk, deferred until multiple
  app containers are actually required.

Phase 1 alone is low-risk and non-breaking — a good first move that lays the
groundwork without changing any current behavior.
