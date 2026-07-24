# Reporting — Design & Build Plan

**Goal:** Give managers and SEs a **Salesforce-style report layer**: build a report against POC data, filter/group/sort it, **run and save** it, **export** it, **publish** curated reports to an audience, and have reports **emailed on a schedule**. All of it enforces the existing region + capability RBAC, and none of it destabilizes the single-writer app.

**What exists today (the foundation):** Rich *per-project* exports already exist — HTML, PDF (WeasyPrint), DOCX, PPTX readout, XLSX tracker, zip archive, AI narrative ([report_routes.py](../app/ui/report_routes.py)) — plus a portfolio **analytics** page (win/loss, cycle time, region rollups) via [insights.py](../app/services/insights.py). We also already have an **SMTP** service ([email.py](../app/services/email.py), today only for invites) and an in-process **background-loop** pattern ([main.py](../app/main.py)). What's missing is a **user-defined, saved, schedulable** report layer on top. This plan builds that — reusing the exporters, RBAC, SMTP, and loop pattern rather than adding a BI tool.

**Why not a BI container (Metabase / Superset / Redash):** They connect *directly to the database*, which (a) bypasses our region RBAC entirely — they'd see every row unless we painstakingly re-implement the region model inside them — and (b) on SQLite would mean a second concurrent process on the file (the "disk I/O error" failure mode). Even after Postgres, the access-model duplication is a real ongoing governance cost for client POC data. **We build reporting in-app**, where it already understands our models, RBAC, branding, and exporters. An external BI tool is reconsidered only if genuine ad-hoc-SQL demand emerges (Phase 4).

**Decisions locked in:**
- **Built on Postgres.** Reporting is the most DB-divergent feature we have (JSONB filters, FTS, aggregation, read concurrency / replicas). It is implemented **on Postgres only** — it depends on [`postgres-migration-plan.md`](./postgres-migration-plan.md) Phase 5. Design (this doc's Phase 0) is engine-agnostic and can proceed in parallel now.
- **Declarative query builder, never user SQL.** Reports are defined by a **field registry** (whitelisted, indexed fields per entity) + JSON filters, compiled to SQLAlchemy. No raw SQL from users — safe, testable, and automatically routed through the region access helpers.
- **RBAC is non-negotiable and reused.** Every report runs through `accessible_project_ids` ([access.py](../app/services/access.py)) so a manager sees only their regions and an SE only theirs. New capabilities gate the *actions* (`report.view/create/publish/schedule`), orthogonal to region scope — consistent with [`rbac-role-builder-plan.md`](./rbac-role-builder-plan.md).
- **Two use cases, one model, distinguished by visibility.** *"Admin/manager builds and publishes to an audience"* = visibility `published`; *"user creates their own"* = visibility `private`. Same `SavedReport` row, different `visibility` + `audience`.
- **Reuse the export pipeline.** Run a report → hand the result set to the existing XLSX/PDF/CSV exporters. Export is nearly free.
- **Stability discipline (the real risk, not "which tool"):** reports are **strictly read-only**; heavy renders (WeasyPrint, big XLSX) run **off the event loop** (`asyncio.to_thread` / `run_in_threadpool`) behind a **small concurrency semaphore**; every query is **bounded** (result cap, pagination, max date window, whitelisted indexed fields only). A runaway report can't freeze requests or scan the DB unbounded.

**Stack reminder:** FastAPI + SQLAlchemy 2.0 + Jinja. Region RBAC choke points in [access.py](../app/services/access.py)/[scope.py](../app/services/scope.py). Capability model in [capabilities.py](../app/services/rbac/capabilities.py) (`resource.action` keys, gated by `rbac_dynamic_enabled`). Next free Alembic revision is `0043` (coordinate with the RBAC + Postgres efforts).

---

## Design summary

Three building blocks on top of Postgres:

| Block | What it is | Reuses |
|---|---|---|
| **Field registry** | Per-entity catalog of queryable fields (type, operators, whether filterable/groupable/indexed) | new — the safety boundary |
| **`SavedReport` model** | owner, entity, `filters` (JSONB), columns, group/sort, `visibility`, `audience`, schedule | new tables only |
| **Runner + exporters** | compile registry+filters → SQLAlchemy (region-scoped, bounded) → HTML/charts + existing exporters | `access.py`, existing report exporters |

**`SavedReport` sketch:**

```
saved_reports
  id, owner_id (FK app_users)
  name, description
  entity            -- 'project' | 'task' | 'use_case'  (registry key)
  filters           -- JSONB: [{field, op, value}, ...]  (fields validated against registry)
  columns           -- JSONB: ordered list of registry field keys
  group_by, sort    -- JSONB
  visibility         -- 'private' | 'shared' | 'published'
  audience           -- for 'shared': role/region ids; for 'published': 'client' | 'internal'
  schedule          -- nullable JSONB: {cron, recipients, format}  (Phase 3)
  created_at, updated_at
```

**Field-registry sketch** (the load-bearing safety decision — mirrors the RBAC capability registry pattern):

```python
# app/services/reporting/registry.py
@dataclass(frozen=True)
class ReportField:
    key: str            # 'project.status', 'project.region', 'usecase.completion_pct'
    label: str
    type: str           # 'string' | 'number' | 'date' | 'enum' | 'bool'
    ops: tuple[str]     # allowed operators, e.g. ('eq','in','contains','between')
    filterable: bool
    groupable: bool
    indexed: bool       # only indexed fields may be filtered/sorted at scale
```

A filter is accepted only if its `field` is in the registry and its `op` is in that field's `ops`. This is what makes user-built reports safe **and** performant — no unindexed scans, no injection surface.

---

## PHASE 0 — Design finalize (engine-agnostic — can start now)

Runs in parallel with the Postgres migration; produces reviewable specs, no production code on SQLite.

- [ ] **0.1 — Field registry v1.** Enumerate the reportable fields for `project`, `task`, `use_case` (reuse [insights.py](../app/services/insights.py) derivations — completion %, at-risk, stalled, outcome, cycle time). Mark `indexed`; note which need new Postgres indexes.
- [ ] **0.2 — `SavedReport` schema + visibility/audience model.** Finalize the table (above) and the visibility semantics (private / shared-to-role-or-region / published-to-audience).
- [ ] **0.3 — Capability additions.** Add `report.view`, `report.create`, `report.publish`, `report.schedule` to [capabilities.py](../app/services/rbac/capabilities.py) (seeded into the appropriate roles). Confirm they compose with region scope per the role-builder plan.
- [ ] **0.4 — UX wireframes.** Report list, builder (pick entity → columns → filters → group/sort), run view, save/share dialog, schedule dialog.

---

## PHASE 1 — Query builder + run/view/export (on Postgres)

The core. All **read-only**, **region-scoped**, **bounded**, **thread-pooled**.

- [ ] **1.1 — Migration `00xx`.** `saved_reports` table (new table only — no `projects` touch). Add any Postgres indexes the registry marks needed.
- [ ] **1.2 — Registry → SQLAlchemy compiler.** Compile `(entity, filters, columns, group_by, sort)` into a query, **always** intersected with `accessible_project_ids(db, user)`. Enforce result cap + pagination + max window.
- [ ] **1.3 — Run view.** HTML table + a couple of charts. Server-rendered, reusing the app's branding/templates.
- [ ] **1.4 — Export.** Wire the result set into the existing XLSX/PDF exporters; add CSV. Heavy renders via `asyncio.to_thread` behind a concurrency semaphore.
- [ ] **1.5 — Tests.** Region enforcement (SE sees only their region; manager their regions), filter/op validation rejects off-registry fields, result bounding.

---

## PHASE 2 — Save / share / publish

- [ ] **2.1 — CRUD + ownership.** Create/edit/delete own reports (`report.create`). List shows own + shared-to-me + published.
- [ ] **2.2 — Visibility enforcement.** `private` (owner only), `shared` (role/region audience), `published` (`report.publish`, curated catalog). Publishing to an audience still re-runs region scoping **per viewer** — a published report never leaks rows across regions.
- [ ] **2.3 — Audience = client/internal.** Reuse the existing report audience concept ([report_routes.py](../app/ui/report_routes.py) `_resolve_include_internal`) so internal-only fields never surface to a client audience.

---

## PHASE 3 — Scheduled email delivery

- [ ] **3.1 — Schedule model.** `schedule` JSONB on `saved_reports` (cron, recipients, format).
- [ ] **3.2 — Scheduler loop.** A new background loop in the [main.py](../app/main.py) pattern that wakes, finds due reports, renders, and emails via [email.py](../app/services/email.py). **Guarded by a Postgres advisory lock** (one runner across N tasks — same mechanism as [`postgres-migration-plan.md`](./postgres-migration-plan.md) Phase 6.1).
- [ ] **3.3 — Stability caps.** Cap concurrent renders (1–2), per-run timeout, and one failed report must not kill the loop (mirror `_audit_retention_loop` resilience). Renders run off the event loop.
- [ ] **3.4 — Audit + delivery record.** Record each scheduled send (success/failure) via [audit.py](../app/services/audit.py).

---

## PHASE 4 — Scale-out (optional, only when justified)

- [ ] **4.1 — Read replica routing.** Route report queries to an RDS **read replica** (Postgres plan 6.5) so heavy reads never touch the transactional path.
- [ ] **4.2 — Dedicated reporting worker (if load demands).** Run **our own codebase** in a worker role (no web traffic) for scheduled/heavy renders — it already understands our RBAC, models, and exporters. Preferable to any external tool.
- [ ] **4.3 — External BI (only if ad-hoc-SQL demand emerges).** Reconsider Metabase/Superset pointed at the **read replica**, *with* a plan to reconcile region access (Postgres row-level security or per-region DB roles). Not before there's real demand the in-app builder can't meet.

---

## Sequencing

Phase 0 (design) starts **now**, in parallel with the Postgres migration. Phases 1–3 begin once Postgres cutover ([`postgres-migration-plan.md`](./postgres-migration-plan.md) Phase 5) lands. Phase 4 is demand-driven. Reporting is deliberately built **once, on Postgres** — never on SQLite.
