# Postgres Migration — Execution Plan

> **Update (2026-07-25): the decision changed to Postgres-ONLY.** The dual-DB
> phrasing below (SQLite as the default, Postgres opt-in) was superseded — SQLite
> was fully removed from the runtime and the tests. The app defaults to Postgres,
> `docker compose up` runs it automatically, tests + CI are Postgres-only, and the
> AWS `deploy.sh` provisions RDS. See [`postgres-cutover-runbook.md`](./postgres-cutover-runbook.md).
> The phase history below is retained as the record of how the migration was built.

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

## PHASE 0 — Foundation (dialect-configurable engine) ✅

App can *boot* against an empty Postgres. Fully backward-compatible with today's SQLite setup.

- [x] **0.1 — Configurable DB URL.** ✅ Added `POCT_DATABASE_URL` (via `database_url_override`, `validation_alias`) + `db_pool_size` / `db_max_overflow` / `db_pool_recycle_seconds` to [config.py](../app/config.py). `database_url` returns the override when set, else the SQLite default; added an `is_sqlite` helper. `database_path` kept for the SQLite-only file helpers.
- [x] **0.2 — Dialect-branched engine.** ✅ [db.py](../app/db.py) `_build_engine` now branches on `settings.is_sqlite` → `_build_sqlite_engine` (unchanged `check_same_thread` + WAL/FK/busy-timeout PRAGMAs) vs `_build_postgres_engine` (`pool_pre_ping`, `pool_size`, `max_overflow`, `pool_recycle`). `ensure_data_dir()` still runs for both (session/JWT keys live under data_dir regardless of DB).
- [x] **0.3 — Driver + local Postgres.** ✅ Added `psycopg[binary]>=3.2` to `pyproject.toml`; added a `postgres` service (Postgres 16, `pg_isready` healthcheck, `POCT_PG_HOST_PORT`) + `poct-pgdata` volume to the existing `docker-compose.yml`.
- [x] **0.4 — Smoke test.** ✅ Full suite green on SQLite (**458 passed**, 2 skipped; the 1 failure — `test_project_report_html_and_zip` — is **pre-existing on `main`**, unrelated). Postgres branch verified live against the compose DB: `is_sqlite=False`, dialect `postgresql`, `QueuePool`, `SELECT version()` → PostgreSQL 16.14. Full `alembic upgrade head` on Postgres is deferred to Phase 1 (FTS5 + `0026` raw SQL still error on PG). Tests kept hermetic on SQLite via a conftest `delenv` of `POCT_DATABASE_URL`.

---

## PHASE 1 — Dialect-aware migrations ✅

`alembic upgrade head` runs clean on **both** databases.

- [x] **1.1 — Conditional batch mode.** ✅ [env.py](../alembic/env.py) now sets `render_as_batch` from `_render_as_batch = database_url.startswith("sqlite")` in both offline + online configure calls.
- [x] **1.2 — Branch raw SQL by dialect.** ✅ Two kinds of breakage found + fixed across the 5 raw-SQL migrations:
  - **FTS ([0012](../alembic/versions/0012_add_search_index.py))** — split into `_upgrade_sqlite` (unchanged FTS5 virtual table + `BEGIN…END` triggers) and `_upgrade_postgres` (real `search_index` table + `tsvector` column, **GIN index**, unique `(entity_type, entity_id)`, and one `plpgsql` `AFTER INSERT/UPDATE/DELETE` function+trigger per entity; title→weight A, body→weight B). This lands the Phase-2 *index* early so the search rewrite is just the query branch.
  - **Boolean `= 1` literals** in [0017](../alembic/versions/0017_add_library_sets.py), [0018](../alembic/versions/0018_library_set_default.py), [0026](../alembic/versions/0026_add_external_user_expiry.py), [0033](../alembic/versions/0033_add_win_loss_outcome.py) → portable `true`/`false` (SQLite ≥ 3.23 accepts them; runtime is 3.51). Plus **`0033`**'s SQLite-only `date(updated_at)` → dialect-branched `CAST(updated_at AS date)` on Postgres.
- [x] **1.3 — Verify.** ✅ Fresh Postgres: all 42 migrations → head, seed ran, `GET /health` → `{'database':'ok'}`, FTS triggers populated 13 indexed rows (non-null `tsv`, live `plainto_tsquery` match). Fresh SQLite: `upgrade head` clean; full suite **458 passed / 2 skipped** (the 1 failure is the pre-existing `test_project_report_html_and_zip`, unrelated).

---

## PHASE 2 — Search rewrite (largest item) ✅

Postgres search path behind the existing `search()` interface — callers unchanged.

- [x] **2.1 — Index.** ✅ Landed early in the Phase 1 FTS migration branch: `search_index.tsv` (weighted `tsvector`, title=A/body=B) + **GIN index** + trigger-maintained. Prefix/as-you-type handled by `to_tsquery` `:*` (see 2.2) rather than `pg_trgm`.
- [x] **2.2 — Query branch.** ✅ [search.py](../app/services/search.py) now branches on `db.get_bind().dialect.name`: Postgres uses `to_tsquery('english', :q)` + `ts_rank(tsv, query)` (ORDER BY DESC); SQLite keeps FTS5 `MATCH` + `bm25` (ORDER BY ASC). New `build_tsquery` mirrors `build_match_query`'s token safety (`\w+` only, last token `:*` prefix). `rebuild_index()` branches its INSERT to compute `tsv` on Postgres. All downstream resolution/highlighting/visibility logic is shared.
- [x] **2.3 — Parity tests.** ✅ SQLite `test_search.py` 18 passed. Postgres live: `provisioning`→use_case+library, `Acme`→project+customer, prefix `prov`→match, `account`→ranked multi-hit, `won`→empty; visibility scoping enforced; `rebuild_index` recomputes `tsv` and search still returns hits.

---

## PHASE 3 — Backups ✅

- [x] **3.1 — DB backup path.** ✅ Chose **(b)**: on Postgres the archive is **files + keys only**; DB backup/restore is RDS snapshots + PITR. [backups.py](../app/services/backups.py) gates the DB snapshot/member, restore DB-swap, and safety-snapshot on `settings.is_sqlite`; manifest gains `includes_database` (FORMAT_VERSION→2; v1 archives default True); `validate_archive` skips the DB checksum/schema checks for files-only archives; `backup_includes_database()` drives a Backups-page note. SQLite behavior byte-identical (9 backup tests pass). Verified live on Postgres: archive has no `db/poct.db`, restore leaves the DB intact.

---

## PHASE 4 — Type / tz audit + CI matrix ✅

Postgres is strict where SQLite is loose.

- [x] **4.1 — Datetime + boolean sweep.** ✅ Boolean `= 1` and SQLite-only `date()` in raw-SQL migrations fixed in Phase 1; the rest is ORM-shielded and now **empirically audited by running the whole suite on Postgres** (4.2), which exercises the datetime/boolean/JSON paths on the strict engine.
- [x] **4.2 — Both-DB CI.** ✅ New [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) runs the suite as a matrix on **SQLite and Postgres** (PG 16 service container). [conftest.py](../tests/conftest.py) gained Postgres test mode: session-level reset+migrate + a snapshot of the migration-seeded baseline, then **per-test TRUNCATE + baseline restore** to a clean migrated state (`client` tests re-seed; `db_session` tests build their own); leftover backends are terminated so TRUNCATE can't block, and engines are disposed between tests. One archive-restore test is `skipif`-Postgres (DB restore is out-of-band there). **Result: the full suite passes identically on both engines — Postgres run 456 passed / 3 skipped, the only 2 failures are the pre-existing engine-independent ones that also fail on SQLite `main`.** SQLite path unchanged (default when `POCT_DATABASE_URL` unset).

---

## PHASE 5 — Cutover (production moves to RDS)

> **Operator-executed** (needs AWS access). Full step-by-step in
> [`postgres-cutover-runbook.md`](./postgres-cutover-runbook.md). The **data-migration
> tooling is built + tested**; the RDS/deploy steps are yours to run.

- [ ] **5.1 — Provision RDS** (Postgres, Multi-AZ for HA, automated snapshots + PITR). Wire `POCT_DATABASE_URL` from a secret. *(operator)*
- [x] **5.2 — Data migration.** ✅ [`app/scripts/migrate_sqlite_to_postgres.py`](../app/scripts/migrate_sqlite_to_postgres.py): schema-check, TRUNCATE target, copy every table in FK order (preserving ids), reset sequences, triggers rebuild the search index. Tested locally end-to-end against the compose Postgres — full row parity, search works, sequence reset gives collision-free new ids.
- [ ] **5.3 — Deploy on Postgres, single task first.** *(operator — runbook §4)*
- [ ] **5.4 — Rollback plan.** Unset `POCT_DATABASE_URL` to revert to SQLite until signed off. *(operator — runbook §Rollback)*

---

## PHASE 6 — HA / scale-out hardening

The **code** that makes ≥2 tasks safe is done; the service-count bump + replica are operator steps. Files remain on **EFS** (already shared) — no object-storage rewrite needed.

- [x] **6.1 — Single-runner background jobs.** ✅ New [`db_locks.py`](../app/services/db_locks.py) (`advisory_lock` / `run_singleton`; no-op on SQLite). The three daily sweeps in [main.py](../app/main.py) (loop bodies **and** their startup one-shots) run under non-blocking advisory locks, so only one instance fires them. Verified on Postgres that a held lock makes a second connection skip. (Reporting's scheduler reuses this — [`reporting-plan.md`](./reporting-plan.md) Phase 3.)
- [x] **6.2 — Migration coordination.** ✅ Startup `run_migrations()` runs under a **blocking** advisory lock (`LOCK_MIGRATIONS`); N booting tasks serialize, the rest find the DB at head.
- [x] **6.3 — Cache coherence.** ✅ [system_config.py](../app/services/system_config.py) + [branding.py](../app/services/branding.py) caches gained a 30 s TTL, applied **only on Postgres** (SQLite keeps `invalidate()`-only behavior, byte-identical), bounding cross-instance staleness.
- [ ] **6.4 — Scale the service.** Bump the ECS service to **≥2 tasks behind the ALB**. *(operator — runbook §5)*
- [ ] **6.5 — Read replica (when reporting load justifies).** Add an RDS read replica; route heavy report queries to it (see reporting Phase 4). *(operator)*

---

## Effort

Per `POSTGRES.md`: **Postgres-capable (Phases 0–4) ≈ 2 weeks focused work**, with search (2) and backups (3) dominating. Cutover (5) is a bounded operational task. HA hardening (6) is a smaller, separable chunk — advisory locks + a service-count bump, made easier here because files are already on EFS.

**Sequencing with reporting:** Phases 0–5 block reporting *implementation* but not reporting *design* — the reporting data model and field-registry are engine-agnostic and can be designed in parallel now (see [`reporting-plan.md`](./reporting-plan.md) Phase 0).
