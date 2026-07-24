# Postgres Migration — Execution Plan

**Goal:** Move POC Tracker's primary datastore from SQLite to **managed Postgres (AWS RDS)** as the foundation for (a) a rollout to ~50 SEs + ~30 managers and (b) a first-class reporting layer (see [`reporting-plan.md`](./reporting-plan.md)). Postgres is treated as the **target substrate we build on going forward** — not an opt-in someday.

**Why now (the forcing function):** Two independent pressures converge:
1. **Scale.** At ~80 users the single-writer SQLite path hits write contention, and — just as important — the single-ECS-task constraint (SQLite-on-EFS gives "disk I/O error" with two tasks) means **no HA and downtime on every deploy**. That's a real availability risk for a tool this many people depend on daily.
2. **Reporting.** The report builder is the single most DB-divergent feature we'd add (JSONB filters, full-text search, aggregation, read concurrency / replicas). Building it on SQLite means building our flagship feature on the engine we're about to drop, and re-testing every query on the target anyway. **Postgres goes first so reporting is built once, on rails we trust.**

**Relationship to `POSTGRES.md`:** [`POSTGRES.md`](./POSTGRES.md) is the technical deep-dive — the subsystem-by-subsystem analysis of what breaks and how (FTS5, backups, dialect-aware migrations, type/tz strictness). **This document does not repeat it**; it turns that analysis into a decision-locked, phased execution plan and records the strategic shift (from "dual-DB, SQLite stays default, defer until it bites" → "migrate now, Postgres is the target"). Read `POSTGRES.md` alongside each phase for the mechanics.

**Decisions locked in:**
- **Target = managed Postgres (AWS RDS).** Managed HA, automated snapshots, PITR, and read replicas (the replica matters for reporting) without running our own DB.
- **Keep SQLite as an opt-in local/demo mode** — per the `POSTGRES.md` dual-DB recommendation. Same codebase, dialect-aware, selected by `POCT_DATABASE_URL`. This preserves the zero-dependency dev/demo setup and gives us a both-DB CI matrix that keeps the two honest. We are **not** deleting SQLite; we are changing which DB *production* runs.
- **Multi-task HA is an explicit goal.** Once the DB is off the shared file, run **≥2 ECS tasks behind the ALB** (rolling deploys, no downtime, no SPOF).
- **Uploaded files stay on EFS — no S3 rewrite required.** `POSTGRES.md` §"Scaling out" assumes files live on a *local* volume; in this deployment screenshots, note attachments, the deck template/logo, and backups already sit on **EFS, which is network-shared across tasks**. Moving only the DB to RDS lets 2+ tasks share those files as-is. The remaining multi-task concerns are **job/migration coordination and cache coherence** (§Phase 6), not object storage.

**Stack reminder:** FastAPI + SQLAlchemy 2.0 + Alembic. Migrations run at startup ([main.py](../app/main.py) lifespan). `projects` has FTS triggers (`si_project_*`) — the batch_alter_table caveat. Latest revision is `0042`; **next free revision is `0043`**. Engine/pragmas live in [db.py](../app/db.py) `_build_engine`; connection string in [config.py](../app/config.py) `database_url` (currently hardcoded to SQLite, lines 138–141).

---

## Design summary

Everything routes through SQLAlchemy + Alembic, so the generic "talk to a different DB" swap is ~20% of the work; the SQLite-specific subsystems + ops are ~80% (`POSTGRES.md` breakdown). The code stays **dialect-aware** — one codebase, `POCT_DATABASE_URL` picks the engine.

| Concern | SQLite today | Postgres target | Phase |
|---|---|---|---|
| Connection / engine | hardcoded `sqlite:///…`, SQLite pragmas | `POCT_DATABASE_URL`, dialect-branched engine (`pool_pre_ping`, `pool_size`) | 0 |
| Migrations | `render_as_batch=True`, raw SQLite SQL in FTS + `0026` | conditional batch, dialect-branched raw SQL | 1 |
| Full-text search | FTS5 `MATCH` + `bm25()` ([search.py](../app/services/search.py)) | `tsvector` + GIN + `ts_rank` behind same `search()` interface | 2 |
| Backups | `sqlite3.Connection.backup()` file copy | `pg_dump`/RDS snapshots; in-app archive scoped to files + keys | 3 |
| Type / tz strictness | loose | strict — naive-datetime + boolean audit + both-DB CI | 4 |
| Cutover | — | data load SQLite→RDS, deploy on Postgres | 5 |
| Multi-task HA | single task (file lock) | ≥2 tasks; advisory-lock jobs + migrations; cache TTL; files stay on EFS | 6 |

**Non-breaking until Phase 5.** Phases 0–4 add a Postgres *capability* while production keeps running on SQLite. The cutover (5) and HA (6) are where production actually moves.

---

## PHASE 0 — Foundation (dialect-configurable engine)

App can *boot* against an empty Postgres. Fully backward-compatible with today's SQLite setup.

- [ ] **0.1 — Configurable DB URL.** Add `POCT_DATABASE_URL` to [config.py](../app/config.py); `database_url` returns it when set, else the existing `sqlite:///<data_dir>/poct.db` default. Keep `database_path` for the SQLite-only file helpers (backups, restore).
- [ ] **0.2 — Dialect-branched engine.** In [db.py](../app/db.py) `_build_engine`, apply `check_same_thread` + the `PRAGMA journal_mode=WAL / synchronous / busy_timeout / recursive_triggers` block **only when `dialect == "sqlite"`**. Add a Postgres branch: `pool_pre_ping=True`, sensible `pool_size` / `max_overflow`, `pool_recycle`.
- [ ] **0.3 — Driver + local Postgres.** Add `psycopg` (v3) to `pyproject.toml`; add a `postgres` service to `docker-compose.yml` with a readiness wait on boot.
- [ ] **0.4 — Smoke test.** App boots against an empty Postgres (schema created by Phase 1 migrations). No behavior change on SQLite.

---

## PHASE 1 — Dialect-aware migrations

`alembic upgrade head` runs clean on **both** databases.

- [ ] **1.1 — Conditional batch mode.** [env.py](../alembic/env.py) sets `render_as_batch=True` in two places (lines 39, 57) — a SQLite `ALTER TABLE` workaround. Make it `dialect == "sqlite"` only.
- [ ] **1.2 — Branch raw SQL by dialect.** Any migration with raw SQL branches on `op.get_bind().dialect.name`. Known offenders per `POSTGRES.md`: the FTS setup (`0012` and FTS-touching revisions), and **`0026`** which uses `WHERE is_external = 1` (**errors on Postgres** — boolean ≠ integer).
- [ ] **1.3 — Verify.** `alembic upgrade head` on a fresh Postgres yields the full schema; existing SQLite upgrade path unchanged.

---

## PHASE 2 — Search rewrite (largest item)

Postgres search path behind the existing `search()` interface — callers unchanged.

- [ ] **2.1 — Index.** Replace the FTS5 virtual table + triggers with a Postgres `tsvector` (generated column or trigger-maintained) + **GIN index**. Consider `pg_trgm` for the as-you-type prefix behavior that FTS5 `*` gives today.
- [ ] **2.2 — Query branch.** In [search.py](../app/services/search.py), branch the raw query (`search_index MATCH :q`, `bm25(...)`) by dialect: Postgres uses `plainto_tsquery`/`to_tsquery` + `ts_rank`. Keep `build_match_query`'s token-safety guarantees. `rebuild_index()` gets a Postgres path too.
- [ ] **2.3 — Parity tests.** Same query fixtures produce equivalent ranked results on both engines.

---

## PHASE 3 — Backups

- [ ] **3.1 — DB backup path.** With Postgres there's no file to copy ([backups.py](../app/services/backups.py) uses `sqlite3.Connection.backup()`). Decide: (a) a `pg_dump`/`pg_restore`-aware path, or **(b, recommended)** scope the in-app archive to **files + keys only** and hand DB backup/restore to **RDS automated snapshots + PITR**. The startup file-swap restore ([main.py](../app/main.py) lifespan) no longer applies to the DB under (b).

---

## PHASE 4 — Type / tz audit + CI matrix

Postgres is strict where SQLite is loose.

- [ ] **4.1 — Datetime + boolean sweep.** Audit naive vs tz-aware datetimes (the `.replace(tzinfo=UTC)` coercions) and `0/1`-as-boolean in any raw SQL / comparisons. The ORM shields most of this.
- [ ] **4.2 — Both-DB CI.** Run the **full test suite against Postgres too**, as a matrix, so the two engines can't silently drift. This is the guardrail that makes dual-DB safe.

---

## PHASE 5 — Cutover (production moves to RDS)

- [ ] **5.1 — Provision RDS** (Postgres, Multi-AZ for HA, automated snapshots + PITR). Wire `POCT_DATABASE_URL` from a secret.
- [ ] **5.2 — Data migration.** One-time SQLite → Postgres load (script over the ORM, or `pgloader`). Verify row counts + spot-check FKs, JSON columns, and search results post-load.
- [ ] **5.3 — Deploy on Postgres, single task first.** Point the existing single ECS task at RDS; keep EFS for files. Validate the app end-to-end (search, reports, exports, background sweeps) before scaling out.
- [ ] **5.4 — Rollback plan.** Keep the last SQLite snapshot; cutover is reversible by flipping `POCT_DATABASE_URL` back until 5.3 is signed off.

---

## PHASE 6 — HA / scale-out hardening

Only after 5 is stable. This is what unlocks ≥2 tasks. Files remain on **EFS** (already shared) — no object-storage rewrite needed.

- [ ] **6.1 — Single-runner background jobs.** The in-process daily sweeps (`_audit_retention_loop`, `_external_expiry_loop`, `_google_sync_loop` in [main.py](../app/main.py)) would run N times with N tasks (duplicate expiry emails, etc.). Guard each with a **Postgres advisory lock** so exactly one task runs them. (The reporting scheduler in [`reporting-plan.md`](./reporting-plan.md) Phase 3 uses the same lock pattern.)
- [ ] **6.2 — Migration coordination.** Startup migrations would race across N booting tasks — wrap in an advisory lock, or run migrations as a separate deploy step.
- [ ] **6.3 — Cache coherence.** In-process caches (branding, system settings) are per-task. Add a short TTL, or `LISTEN/NOTIFY`; for rarely-changed settings a small TTL is acceptable.
- [ ] **6.4 — Scale the service.** Bump the ECS service to **≥2 tasks behind the ALB**; confirm rolling deploys have zero downtime and both tasks serve EFS-hosted files.
- [ ] **6.5 — Read replica (when reporting load justifies).** Add an RDS read replica; route heavy report queries to it (see reporting Phase 4).

---

## Effort

Per `POSTGRES.md`: **Postgres-capable (Phases 0–4) ≈ 2 weeks focused work**, with search (2) and backups (3) dominating. Cutover (5) is a bounded operational task. HA hardening (6) is a smaller, separable chunk — advisory locks + a service-count bump, made easier here because files are already on EFS.

**Sequencing with reporting:** Phases 0–5 block reporting *implementation* but not reporting *design* — the reporting data model and field-registry are engine-agnostic and can be designed in parallel now (see [`reporting-plan.md`](./reporting-plan.md) Phase 0).
