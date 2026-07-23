# Region-Based RBAC — Implementation Plan

**Goal:** Roll POC Tracker out to SEs globally with region-scoped access. Add a **Manager** role.

**Decisions locked in:**
- SE visibility = **hard boundary** (an SE sees/edits only their region's POCs; today they can see everything).
- Managers = **view AND edit** across their assigned regions (can span multiple regions).
- Role & region = **app-managed** for now (admin sets them in the users UI, like `is_admin` today). Authentik-claim sync is a future, additive enhancement.

**Stack reminder:** FastAPI + SQLAlchemy 2.0 + SQLite + Alembic. Migrations run at startup. `projects` table has FTS triggers — never use `batch_alter_table` on it.

---

## Design summary

Every non-admin, non-external user carries a **set of regions they can view + edit**. That single derived set drives all enforcement.

| Role | `allowed_region_ids` | Notes |
|------|----------------------|-------|
| `admin` | `None` (= all) | Unchanged global god-mode. |
| `manager` | their `user_regions` set (N regions) | View + edit across those regions. |
| `standard` (SE) | their `user_regions` set (1 region) | Hard boundary. |
| `external` | n/a — uses `ProjectGrant` | Region logic bypassed; unchanged. |

Enforcement funnels through the choke points that already exist:
[`app/services/access.py`](../app/services/access.py) and [`app/services/scope.py`](../app/services/scope.py) for reads, plus **new** write guards on project create/update/grant.

---

## PHASE 0 — Data model & migrations (foundation)

> All migrations use plain `op.add_column` / `op.create_table` — **no `batch_alter_table` on `projects`** (drops FTS triggers si_project_*). Template: `alembic/versions/0031_add_project_type.py`.

- [x] **0.1 — `regions` lookup table + model.** ✅ *(migration `0036`; seeds system "Unassigned"; FTS triggers verified intact)*
  New model `app/models/region.py` (`id`, `name`, `sort_order`, optional `description`, `is_active`). Follows the existing lookup pattern.
  Migration: `op.create_table("regions", ...)`. Seed a default/"Unassigned" region.

- [x] **0.2 — `role` on `AppUser`.** ✅ *(approach adjusted — see note)*
  **Adjusted from the original plan.** The codebase (8 app write sites, ~8 query-expression sites, and **47 test references**) constructs and queries `is_admin`/`is_external` as real columns. Converting them to read-only property shims would have broken all of that. Instead:
  - Kept `is_admin` / `is_external` as real stored columns (untouched).
  - Added one stored `is_manager` boolean (migration `0037`, `op.add_column` + `server_default='0'` — backfills existing rows to False).
  - Added a `role` **property with getter + setter** (`admin`/`manager`/`standard`/`external`) plus `ROLE_*` constants and `VALID_ROLES` in `app/models/app_user.py`. Getter resolves the booleans by precedence (admin > external > manager > standard) so no combo is invalid; setter maps a role name deterministically back to the flags and raises on unknown values.
  - Net effect: single clean read/write accessor (`user.role`) with **zero breakage** — all existing queries, writes, and tests keep working. Enum-style cleanup of scattered `is_admin` reads still deferred to Phase 3.4.

- [x] **0.3 — `user_regions` join table.** ✅ *(migration `0038`, `UserRegion` model; insert / both cascades / unique guard verified)*
  `user_regions (user_id FK, region_id FK, primary key (user_id, region_id))`. ORM relationship on `AppUser.regions`. Enforce FK at ORM level.
  Migration: `op.create_table`.

- [x] **0.4 — `region_id` on `Project`.** ✅ *(migration `0039`, plain add_column; FTS triggers verified intact; full suite 391 passed)*
  `op.add_column("projects", Column("region_id", ...))` + `op.create_index`. ORM relationship only (no DB-level FK — same constraint as migration 0031). Backfill happens in Phase 4.

- [ ] **0.5 — Retire boolean flags (after refactor in Phase 3 lands).**
  Once all reads/writes go through `role`, drop the `is_admin`/`is_external` shims (or keep as compatibility props — decide at the time). Update `oidc.py` provisioning to set `role` instead of `is_external`.

**Migration ordering:** 0.1 → 0.2 → 0.3 → 0.4 as separate numbered alembic revisions (next free number is 0036+). Phase 4 backfill is its own revision.

---

## PHASE 1 — Role/region admin management

- [ ] **1.1 — Regions CRUD in admin UI.**
  Add `regions` to the lookups admin surface (`app/ui/lookup_routes.py` `LOOKUPS` dict + `app/api/v1/lookups.py`). Admin-only. Pure reuse of existing lookup machinery.

- [ ] **1.2 — Role selector in the users admin UI.**
  User management lives in `settings_routes.py` + `app/templates/settings/admin_users.html` (no dedicated user_routes file). Replace the admin/external checkboxes with a **role dropdown**.

- [ ] **1.3 — Region assignment in the users UI.**
  Multi-select of regions per user (writes `user_regions`). For a standard SE, constrain/validate to exactly one; for a manager, allow many. Admin-only.

- [ ] **1.4 — Bulk assign / CSV import (scale helper).**
  Optional but recommended before global rollout: bulk-set region for many users at once. Can defer if initial user count is small.

---

## PHASE 2 — Read enforcement

- [ ] **2.1 — `allowed_region_ids(user)` helper.**
  New single source of truth (put in `access.py`): returns `None` for admin, the `user_regions` set for manager/standard, and signals "use grants" for external.

- [ ] **2.2 — Wire into `access.py`.**
  `accessible_project_ids` and `can_view_project` return the region-filtered project set for non-admins instead of `None`. External path unchanged.

- [ ] **2.3 — Wire into `scope.py`.**
  `scoped_project_ids`: the `"all"` scope for a standard/manager user must intersect with `allowed_region_ids` (no more true global view). `"mine"` still narrows to their own assignments. Managers get a region rollup here.

- [ ] **2.4 — Search & dashboard & reports.**
  Verify `search_routes.py`, `dashboard_routes.py`, `report_routes.py` all flow through `access.py`/`scope.py` (they should) so no surface leaks cross-region rows. Audit each list query.

---

## PHASE 3 — Write enforcement (the new surface)

> Today standard users are trusted to write globally. Hard boundaries mean **writes need guarding too**, not just reads.

- [ ] **3.1 — Guard project create.**
  `create_project` (`project_routes.py:494`): the new project's `region_id` must be in the creator's `allowed_region_ids` (or forced to the SE's own region). Reject/redirect otherwise.

- [ ] **3.2 — Guard project update / reassign.**
  `update_project` (`project_routes.py:821`): block edits to projects outside `allowed_region_ids`; validate any region change and SE reassignment stays within the actor's allowed set (managers can move within their regions; SEs cannot move a POC out of their region).

- [ ] **3.3 — Guard grants & sub-resources.**
  `can_grant_project` (`access.py:80`) and write paths for tasks/notes/use-cases must respect region boundaries, not just `is_admin or sales_engineer_id == user.id`.

- [ ] **3.4 — Enum refactor cleanup.**
  Replace direct `is_admin` / `is_external` checks with `role`-based checks across the ~10 python files + 11 templates. Mechanical but touch-everything; do it as one focused pass with the shims still in place as a safety net.

---

## PHASE 4 — Backfill existing data

- [ ] **4.1 — Derive project regions.**
  Data migration: set each existing `Project.region_id` from its assigned SE's region. Orphans (no SE, or SE with no region) → the "Unassigned" region bucket.

- [ ] **4.2 — Assign regions to current users.**
  One-time script/UI pass to give every existing internal user a region before hard boundaries flip on. **Critical:** any user without a region sees nothing (except admins).

- [ ] **4.3 — Safety net.**
  Consider a feature flag / config toggle to enable hard boundaries only after backfill is verified in prod, so enforcement doesn't blank out the app mid-rollout.

---

## PHASE 5 — Manager experience

- [ ] **5.1 — Manager reporting rollups.**
  Region-level views/reports aggregating across a manager's regions (win/loss, milestones — reuse existing report machinery, scoped to `allowed_region_ids`).
- [ ] **5.2 — Region column/filter in project list & search.**
  Surface region as a visible, filterable dimension now that data carries it.

---

## PHASE 6 — Testing & rollout

- [ ] **6.1 — Access-control tests.**
  Matrix tests: SE cannot read/write out-of-region; manager can across their regions only; admin global; external unchanged. Cover read AND write paths.
- [ ] **6.2 — Migration test on a prod DB copy.**
  Verify FTS triggers survive (`si_project_*` still present after `region_id` add) and backfill lands correctly.
- [ ] **6.3 — Staged rollout.**
  Backfill → verify → flip hard-boundary flag → onboard new regions.

---

## LATER (optional) — Authentik claim sync

- [ ] **L.1** Extend `app/services/oidc.py` `find_or_create_user` to read region/group claims and populate `user_regions` at login. Additive — same tables, just auto-populated instead of hand-set. Design in Phase 0 keeps this a non-rewrite.

---

## Suggested build order

**Foundation first, enforcement last:** Phase 0 → Phase 1 → **Phase 4 (backfill)** → Phase 2 → Phase 3 → Phase 5 → Phase 6.
Rationale: get the schema + data populated *before* turning on enforcement, so the app never blanks out. The enum refactor (3.4) can run in parallel with Phase 1/2 behind the compatibility shims.
