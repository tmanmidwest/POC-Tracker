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

- [x] **1.1 — Regions CRUD in admin UI.** ✅ Added `regions` to both lookup surfaces (`lookup_routes.py` `LOOKUPS` + `api/v1/lookups.py` `regions_router` + `schemas/lookups.py`). Added a `references`-based delete guard to the UI `delete_row` (blocks deleting a region still referenced by projects or `user_regions` — needed because `projects.region_id` has no DB FK and `user_regions` cascades). Verified in browser.

- [x] **1.2 — Role selector in the users admin UI.** ✅ Dropdown now offers Standard / Manager / Admin (list page + new-user form). `change_role` and create handlers use the `AppUser.role` setter; guards kept (min one admin, seeded stays admin, no self-change). Verified in browser.

- [x] **1.3 — Region assignment in the users UI.** ✅ Region checkboxes on the user edit page (shown for standard/manager; admins get a "sees all" note, externals none). New `app/services/regions.py` (`get_user_region_ids` / `set_user_regions`) reconciles `user_regions`. A `region_form` marker means unrelated edits (admins/externals) never touch memberships. Verified in browser.

- [x] **1.4 — Bulk assign / CSV import (scale helper).** ✅ New **Settings → Users → Bulk assign regions** page (`/ui/settings/bulk-regions`): an interactive grid (users × regions checkboxes, "Save all") plus CSV import (`identifier,regions`; regions in-cell split on `;`/`|`; header optional; replaces each user's set; skips admins/externals; reports unmatched / unknown-region / skipped). Logic in `app/services/regions.py` (`parse_region_csv`, `bulk_set_regions`). Verified in browser + tests.

---

## PHASE 2 — Read enforcement

- [x] **2.1 — helper + flag gate.** ✅ `region_scoped(user)` (internal, non-admin, enforcement on) and `allowed_region_ids(db, user)` in `access.py`. Everything gates on `system_config.region_enforcement_enabled()` — off = legacy (internal users see all).

- [x] **2.2 — Wire into `access.py`.** ✅ `accessible_project_ids` returns the region-filtered set (projects in the user's regions ∪ their own assignments) for region-scoped users; `None` (all) for admins / enforcement-off; grants for external. `can_view_project` mirrors it (own assignment always visible; region-less projects hidden from non-admins).

- [x] **2.3 — Wire into `scope.py`.** ✅ `scoped_project_ids` now computes the view-scope candidate then **intersects** it with `accessible_project_ids`, so `"all"` can never widen past the region boundary. Proven in the browser: an AMER SE on "All Projects" sees only the AMER POC.

- [x] **2.4 — Audit read surfaces.** ✅ Confirmed dashboard, search (honors `visible_project_ids`), reports, tasks, project list/detail all funnel through the helpers. **Found & fixed two leaks:** customer-detail project list (`customer_routes.py`) and win/loss analytics aggregate (`report_routes.py`) both now filter by `accessible_project_ids`.

---

## PHASE 3 — Write enforcement (the new surface)

> Today standard users are trusted to write globally. Hard boundaries mean **writes need guarding too**, not just reads.

- [x] **3.1 — Guard project create.** ✅ `create_project` + the New POC **wizard** both call `_apply_region_and_check`: region auto-derives from the assigned SE (`regions.sync_project_region`), defaults to a region-scoped creator's sole region, and is rejected if it falls outside their regions (`access.can_use_region`).

- [x] **3.2 — Guard project update / reassign.** ✅ Every mutating route now loads via an **edit-guarded** `_get_project(db, id, user)` (was existence-only `_get_project` → renamed `_load_project`), so out-of-region projects 404. `update_project` re-runs `_apply_region_and_check` after SE reassignment, so a user can't move their own POC out of their regions.

- [x] **3.3 — Guard grants & sub-resources.** ✅ `can_grant_project(db, user, project)` is region-aware (managers/SEs can grant in-region). Sub-resources addressed by their own id — `_get_use_case` and `_get_note` — now enforce parent-project edit access (closed two real holes: top-level `/use-cases/{id}/status` and `/notes/{id}/edit|delete`). Tasks already region-guard project links via `_validate_project`.

- [~] **3.4 — Enum cleanup.** *Intentionally minimal.* The `role` property/setter (Phase 0.2) already unify `is_admin`/`is_external`/`is_manager`; there are no property "shims" to retire. A mechanical sweep of scattered `is_admin` reads is cosmetic, zero-behavior-change, and risky — deferred. New code uses `user.role`.

**Design boundary:** region RBAC governs interactive **UI users**. The REST API / MCP surfaces authenticate as API-key/OAuth **principals** (not region-scoped `AppUser`s) and keep their existing key-based scoping — enforcement is not applied there by design.

---

## PHASE 4 — Backfill existing data

- [x] **4.1 — Derive project regions.** ✅ `backfill_project_regions(db)` in `app/services/regions.py` sets each `Project.region_id` from its SE's region (only when the SE has exactly one region); orphans (no SE, or ambiguous multi-region SE) → the system "Unassigned" bucket. Admin-triggered via **Settings → System → "Run backfill now"** (`POST /ui/settings/system/backfill-regions`), reports counts, idempotent, re-runnable after SE reassignment. *(Chose an admin action over a migration: SE regions are assigned at runtime, so a deploy-time migration would just park everything in Unassigned.)*

- [~] **4.2 — Assign regions to current users.** Runtime admin task, no code — done via **Users → Bulk assign regions** (grid or CSV, built in 1.4). Flagged in the System page + enforcement-toggle hint so an admin does it before flipping the switch.

- [x] **4.3 — Safety net (enforcement flag).** ✅ `AppConfig.region_enforcement_enabled` (default **False**, migration `0040`) — the master switch access.py/scope.py will read. Toggle on **Settings → System**; `system_config.region_enforcement_enabled()` accessor + `set_region_enforcement_enabled()`. Kept off until regions/backfill verified, so enabling can't blank out the app.

---

## PHASE 5 — Manager experience

- [x] **5.1 — Manager reporting rollups.** ✅ Win/Loss Analytics (`report_routes.py`) now computes a **By Region** breakdown — the same win/loss math sliced by region (total / open / won / lost / win-rate), scoped to the viewer's accessible projects, Unassigned bucket last. Shown only when >1 region is represented. Rendered as a full-width table in `reports/analytics.html`.
- [x] **5.2 — Region column/filter in project list.** ✅ Project list (`project_routes.list_projects` + `projects/list.html`) gains a **Region column** and a **Region filter** dropdown, both shown only when regions exist. The dropdown offers the regions the viewer can see (all for admins; their own set otherwise). Search already region-scopes results (Phase 2); a visible region badge there is a nice-to-have, not done.

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
