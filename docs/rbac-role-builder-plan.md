# Dynamic RBAC — Role Builder Plan

**Goal:** Replace the four hardcoded, mutually-exclusive roles (`admin`, `manager`, `standard`/SE, `external`) with **admin-defined roles** whose permissions are configured in the UI. An admin can create a role, tick the capabilities it grants, and assign it to users — without a code change.

**Decisions locked in** (confirmed during scoping):
- **Multiple roles per user.** A user can hold several roles; their effective permission set is the **union** of the roles' capabilities. Modeled by a `user_roles` join table.
- **Fine, per-action capabilities.** Capabilities are `resource.action` (e.g. `project.edit`, `note.view_internal`, `user.change_role`), not coarse buckets. See the catalog below.
- **Admin & External are seeded but protected.** They ship as system roles that can't be deleted or edited past their invariants. **Admin** carries an `is_superuser` flag (implicitly passes every capability check). **External** stays wired to the existing identity machinery (`ProjectGrant`, expiry, invitations, OIDC, internal-note hiding).
- **Capabilities are orthogonal to region RBAC.** Capabilities answer *what actions* a user may take (global); region membership answers *which projects* they may touch. A region-scoped edit requires **both** `project.edit` **and** the project being in-region. The region effort (see [`rbac-region-plan.md`](./rbac-region-plan.md)) is untouched and keeps its own master switch.

**Stack reminder:** FastAPI + SQLAlchemy 2.0 + SQLite + Alembic. Single ECS writer. Migrations run at startup. The role-builder adds **new tables only** (`op.create_table`) and **never touches `projects`**, so the `batch_alter_table`/FTS-trigger caveat (`si_project_*`) does not apply here — but keep it in mind if any follow-up adds a `projects` column. Template revision: `alembic/versions/0037_add_user_manager_role.py`. Next free revision is `0041`.

**Relationship to the region effort:** additive and independent. Region RBAC is Phases 0–5 done, Phase 6 (tests, prod-copy migration test, staged rollout) open, master switch `region_enforcement_enabled` still **OFF**. Nothing here changes those tables, helpers, or the switch. The only touch-point is that a couple of region helpers currently branch on `is_admin`; §3 explains how they migrate to `is_superuser` without breaking the region flag.

---

## Design summary

Three orthogonal axes, kept separate on purpose:

| Axis | Question it answers | Where it lives |
|------|---------------------|----------------|
| **Identity type** (`is_external`) | Grant-based viewer vs internal user? | `app_users.is_external` (unchanged) — drives ProjectGrant visibility, expiry, invitations, OIDC, internal-note hiding |
| **Capabilities** (this plan) | *What actions* may the user take? | `roles` × `role_capabilities` × `user_roles` → `user.can("cap")` |
| **Region scope** (region plan) | *Which projects* may they touch? | `user_regions` + `access.py` helpers (unchanged) |

A write is allowed only when **all applicable axes** agree, e.g. edit a project ⇒ `user.can("project.edit")` **and** `access.can_edit_project(db, user, project)` (region) **and** not `is_external`.

**Enforcement funnels through a single helper — `user.can("capability")` — that replaces the scattered role checks.** During the cutover it's gated by a new master switch `rbac_dynamic_enabled` (default **OFF**), mirroring the region rollout: OFF ⇒ `can()` computes from the legacy `role` so behavior is byte-identical; ON ⇒ `can()` reads the `role_capabilities` union. Because the seed (§4) reproduces today's four roles exactly, flipping the switch is a no-op for existing users.

---

## PHASE 1 — Capability catalog (foundation) ✅

> **Done.** Registry landed at `app/services/rbac/capabilities.py` (package `app/services/rbac/`): 44 capabilities across 8 areas, a frozen `Capability` dataclass, `CAPABILITY_KEYS` / `get_capability` / `is_valid_capability` / `capabilities_by_area` helpers, and import-time integrity guards (unique keys, known areas, `resource.action` slug shape). No DB or enforcement yet — pure code registry. Tests in `tests/test_rbac_capabilities.py` (9, green); ruff clean.

Capabilities are **code-defined**, not admin-defined. A capability only means something if a call site checks it, so admins configure *which roles hold which capabilities*, but the capability set itself is a registry in code (`app/services/rbac/capabilities.py`), reconciled into a `capabilities` table at startup for FK integrity + UI labels/grouping.

Derived from the ~70 real enforcement sites (full catalog produced during scoping). Grouped by area; `resource.action` keys.

### Projects
| Capability | Replaces (today) | Representative sites |
|---|---|---|
| `project.view` | open router; region `can_view_project` | `project_routes.py` detail routes; `access.py:114` |
| `project.create` | `require_internal_ui` | `project_routes.py:518`, wizard |
| `project.edit` | `require_internal_ui` + `can_edit_project` | `project_routes.py:373,388`; `access.py:137` |
| `project.delete` | `require_internal_ui` | `project_routes.py` delete route |
| `project.assign_region` | `can_use_region` (region axis stays separate) | `project_routes.py:355`; `access.py:152` |

> `project.view`/`edit` are the **global** gate; the region helpers (`can_view_project`/`can_edit_project`) remain the **per-project** gate. Both must pass. See §3 for how they compose.

### Use-cases / Notes / Tasks
| Capability | Replaces | Sites |
|---|---|---|
| `usecase.edit` | `require_internal_ui` | `project_routes.py` use-case routes |
| `note.edit` | `require_internal_ui` | `project_routes.py:1856` |
| `note.view_internal` | `user.is_internal` | `visible_project_notes` `access.py:27`; `report_docx.py:253` |
| `note.mark_internal` | inline (internal only) | `project_routes.py:1856-1873` |
| `task.view_own` | task router `internal_only` | `tasks.py` `get_owned_task:82` |
| `task.view_all` | `can_view_all_tasks` (= `is_admin`) | `tasks.py:20`; `task_routes.py:114` |
| `task.edit` | `internal_only` | `task_routes.py:339` |
| `task.mark_internal` | inline | `task_routes.py:473` |

### Customers
| Capability | Replaces | Sites |
|---|---|---|
| `customer.view` | `internal_only` | `customer_routes.py:55` |
| `customer.create` / `customer.edit` / `customer.delete` | `internal_only` | `customer_routes.py:70,132,266` |

### Grants / External sharing
| Capability | Replaces | Sites |
|---|---|---|
| `grant.manage` (grant+revoke) | `can_grant_project` | `access.py:166`; `grant_routes.py:42` |
| `sharelink.manage` | `can_grant_project` | `portal_routes.py:67` |
| `external.extend_expiry` | inline | `grant_routes.py:195` |

> `grant.manage` is the *global* gate; `can_grant_project` keeps the per-project/region logic (own-SE-always, in-region manager). Both must pass.

### Reports / exports
| Capability | Replaces | Sites |
|---|---|---|
| `report.generate` | open router | `report_routes.py` |
| `report.choose_audience` | `user.is_internal` | `report_routes.py:213` |
| `report.export_internal` | `require_internal_ui` | `report_routes.py:153` |

### Admin surfaces
| Capability | Replaces (`require_admin_ui`) | Sites |
|---|---|---|
| `lookups.manage` | admin router | `lookup_routes.py` |
| `library.manage` | admin router | `library_routes.py` |
| `user.view` / `user.create` / `user.edit` / `user.delete` | admin router | `settings_routes.py:240,358,599` |
| `user.change_role` | admin router + inline guards | `settings_routes.py:670` |
| `user.set_password` / `user.unlock` | admin router | `settings_routes.py:426,479` |
| `external_user.manage` (invite/resend/extend/delete) | admin router | `settings_routes.py:508,569` |
| `authprovider.manage` | admin router | `settings_routes.py:1278` |
| `apikey.manage` / `oauthclient.manage` / `mcptoken.manage` | admin router | `settings_routes.py:855,1150,988` |
| `settings.manage` (system/branding/SMTP/demo) | admin router | `settings_routes.py:2357` |
| `feedback.manage` | `require_admin_ui` | `feedback_routes.py:151` |
| `audit.view` | `internal_only` | `audit_routes.py:135` |

### Region administration (region control plane — capability-gated, region logic unchanged)
| Capability | Replaces | Sites |
|---|---|---|
| `region.enforce_toggle` | admin router | `settings_routes.py:1873` |
| `region.backfill` | admin router | `settings_routes.py:1970` |
| `region.assign_users` (bulk grid + CSV) | admin router | `settings_routes.py:762` |

### New meta-capability (introduced by this feature)
| Capability | Meaning |
|---|---|
| `role.manage` | Create/edit/delete roles and assign roles to users. **The privilege-escalation-sensitive one** — see §6. |

### Baseline (implicit, not stored as togglable caps)
`access_ui` (any active user) and `edit_own_profile` (non-external) remain identity checks, not role capabilities — every role has them implicitly. `feedback.submit` is open to all authenticated users.

**Explicitly out of scope:** the REST `/api/v1/*` and MCP surfaces authenticate as API-key/OAuth **principals**, not region-scoped `AppUser`s, and today apply **no** role authz (only the `require_tasks_module` feature flag). Same design boundary as region RBAC (§3.4 of the region plan): capabilities govern **interactive UI users only**. Extending capability enforcement to API/MCP principals is a separate, later effort (noted in §6).

---

## PHASE 2 — Data model ✅

> **Done.** Migration `0041_add_rbac_roles` creates the four tables (all `op.create_table`; `projects` untouched — FTS triggers `si_project_ad/ai/au` verified intact). Models: `app/models/capability.py`, `role.py`, `role_capability.py`, `user_role.py` (registered in `app/models/__init__.py`). `AppUser.roles` is a `viewonly` selectin relationship over `user_roles`; `Role.capabilities` a `viewonly` relationship over `role_capabilities`. Join tables use composite PKs with `ON DELETE CASCADE` (verified). The startup reconciler `app/services/rbac/registry.reconcile_capabilities()` (wired into `seed_database`) upserts the code registry into the `capabilities` table and deletes retired keys; idempotent. Tests in `tests/test_rbac_schema.py` (6, green). No role seeding yet (Phase 4) and no enforcement yet (Phase 3).

Four new tables; `app_users` gains nothing except (optionally, later) a derived read of superuser. Region tables untouched.

```
capabilities            -- reference/registry, seeded from code at startup
  key           TEXT PK           -- "project.edit"
  area          TEXT NOT NULL     -- "Projects" (UI grouping)
  label         TEXT NOT NULL     -- "Edit a POC project"
  description   TEXT
  sort_order    INTEGER

roles
  id            INTEGER PK
  key           TEXT UNIQUE       -- stable slug: "admin","manager","standard","external", then admin-defined
  name          TEXT NOT NULL     -- display name, editable
  description   TEXT
  is_system     BOOLEAN NOT NULL  -- true for the 4 seeded roles; blocks delete + key edit
  is_superuser  BOOLEAN NOT NULL  -- true only for the Admin role; implicitly passes every can()
  is_external   BOOLEAN NOT NULL  -- true only for the External role; marks the read-only identity bundle
  is_active     BOOLEAN NOT NULL DEFAULT 1
  sort_order    INTEGER
  created_at / updated_at

role_capabilities                 -- the role -> permission map
  role_id       INTEGER FK roles(id) ON DELETE CASCADE
  capability_key TEXT FK capabilities(key)
  PRIMARY KEY (role_id, capability_key)

user_roles                        -- the user -> role assignment (MULTIPLE per user)
  user_id       INTEGER FK app_users(id) ON DELETE CASCADE
  role_id       INTEGER FK roles(id) ON DELETE CASCADE
  PRIMARY KEY (user_id, role_id)
```

**FK enforcement** at the ORM level (matching `user_regions` / migration `0038`), with `ondelete` cascades declared. ORM relationships: `AppUser.roles` (many-to-many via `user_roles`), `Role.capabilities` (via `role_capabilities`).

**Why `capabilities` is seeded-from-code, not admin-editable:** a capability is only real if a call site checks it. Admins compose *roles from capabilities*; they can't invent a capability nothing enforces. The `capabilities` table exists for FK integrity + UI labels/grouping; the **source of truth is the code registry** (`app/services/rbac/capabilities.py`), reconciled (upsert new, mark-removed) at startup so adding a capability in code needs no migration.

**Coexistence with the region model:**
- `is_external` **stays a column on `app_users`** — it's an identity discriminator that drives data-model wiring (grants, expiry, invitations, OIDC provisioning, note hiding), not a capability check. The External *role* is the capability bundle that rides along with it; the two are kept in sync (assigning External role ⇒ `is_external=True`, and vice-versa — enforced in the assignment service).
- `is_admin` / `is_manager` booleans **stay during the transition** for backward compatibility (region code, existing queries, 47 test references all read them). Long-term, `is_admin` becomes a derived read of "holds a superuser role"; §3 covers the one region helper that must move from `is_admin` to `is_superuser`.
- `user_regions`, `projects.region_id`, and all `access.py` region helpers are **unchanged**.
- The legacy `AppUser.role` property/setter **stays** as a compat shim (region UI and seeds still use it).

---

## PHASE 3 — Enforcement layer 🟡 (machinery done; call-site cutover staged)

> **Landed:** master switch `rbac_dynamic_enabled` (migration `0042`, `AppConfig` + `system_config`, default **OFF**). `AppUser.can()` / `is_superuser` / `effective_capabilities()` on the model, with `legacy_can` (in `app/services/rbac/defaults.py`) as the switch-OFF fallback so behavior is byte-identical to the four hardcoded roles. `require_capability(cap)` factory in `app/ui/dependencies.py`. Capability checks folded into `access.py` write helpers (`can_edit_project` → `project.edit`, `can_grant_project` → `grant.manage`) and region-bypass moved from `is_admin` → `is_superuser`. Repointed gates: **library** router → `library.manage`, **lookup** router → `lookups.manage`, **feedback board** → `feedback.manage`. Parity tests in `tests/test_rbac_enforcement.py` (5, green); regression across access/region/auth suites (86 green).
>
> **Deliberately deferred (staged per the rollout strategy):** the remaining ~60 call sites — the `settings_routes` user-management / auth-provider / keys / system routes still gate on `require_admin_ui`, and the project/customer/task mutation routes still gate on `require_internal_ui`. While the switch is OFF these are exactly equivalent to their capability form (`legacy_can` governs), so they can be repointed in later waves with zero behavior change. `_legacy_can` is retired only after the switch flips green in staging.

### 3.1 The single helper
```python
# on AppUser (capabilities cached per-request)
def can(self, capability: str) -> bool:
    if not rbac_dynamic_enabled():          # master switch OFF -> legacy behavior
        return _legacy_can(self, capability)  # derived from self.role, byte-identical
    if self.is_superuser:                    # Admin role -> everything
        return True
    return capability in self._effective_capabilities()  # union across user_roles
```
- `is_superuser` = "holds any role with `is_superuser=True`" (memoized per request).
- `_effective_capabilities()` = union of `role_capabilities` across the user's active roles (single query, cached on the request/session-scoped object).
- `_legacy_can()` maps each capability key back to today's `require_admin_ui` / `require_internal_ui` / `is_admin` / `is_internal` logic, so with the switch OFF there is **zero behavior change** — this is the safety net for the call-site migration.

### 3.2 Route-dependency factory (replaces the coarse dependencies)
```python
def require_capability(cap: str):
    def _dep(user: AppUser = Depends(require_ui_user)) -> AppUser:
        if not user.can(cap):
            raise _Forbidden()
        return user
    return _dep
```
- Admin-router routes: `Depends(require_admin_ui)` → `Depends(require_capability("settings.manage"))` etc., per area.
- `require_internal_ui` stays as an **identity** gate (rejects `is_external`) where the concern is "not a read-only viewer"; where a route is really gating a specific action, it moves to `require_capability(...)`. `require_admin_ui` is retired in favor of the factory.

### 3.3 Composing with region (orthogonal, both must pass)
The region helpers keep their per-project logic; the **global capability check moves inside them** so the ~55 project call sites don't each need two checks:
```python
def can_edit_project(db, user, project) -> bool:
    if user.is_external:                     # identity axis
        return False
    if not user.can("project.edit"):         # capability axis (NEW)
        return False
    if not region_scoped(user):              # region axis (unchanged)
        return True
    return can_view_project(db, user, project)
```
One region helper reads identity today and should read capability tomorrow: `region_scoped()` and `allowed_region_ids()` branch on `user.is_admin` to mean "bypasses regions." That should become `user.is_superuser` so a custom role can be granted the region-bypass behavior deliberately. During transition `is_admin ≡ is_superuser` (only the Admin role is superuser and only admins have `is_admin`), so this is a safe, behavior-preserving rename.

### 3.4 Migration path for the ~70 call sites
Staged, low-risk, reversible via the master switch:
1. **Land tables + registry + seed (§2, §4) with the switch OFF.** `can()` returns legacy answers; nothing changes.
2. **Repoint call sites in waves**, area by area, each behind the OFF switch (so `_legacy_can` still governs): admin routers → `require_capability`; project/customer/task mutations → `require_capability` + the updated `access.py` helpers; inline `is_admin`/`is_internal` reads → `user.can(...)` / `note.view_internal` etc.
3. **Test parity** (§7): with the switch OFF, the full suite must pass unchanged (proves `_legacy_can` faithfully reproduces the old gates).
4. **Flip `rbac_dynamic_enabled` ON in staging**, run the same suite (proves the seeded roles reproduce legacy behavior), then production.
5. Once green, `_legacy_can` becomes dead code and can be deleted; `is_admin`/`is_manager` columns can be reconsidered (keep as compat or derive).

The three coarse dependencies collapse into the factory; the region-RBAC helper family stays intact as the per-project axis.

---

## PHASE 4 — Backward-compat seed & migration ✅

> **Done.** `app/services/rbac/defaults.py` centralizes a `LEGACY_TIER` table (every capability → `open`/`internal`/`admin`) and **derives** both the seeded role capability sets and the Phase 3 legacy-parity check from it, so seed and legacy path can't drift. `seed_system_roles` (create-on-first-only; admin=superuser with no rows; manager/SE = internal set; external = open set) and `backfill_user_roles` (zero-roles guard → one-time-per-user, maps `AppUser.role`→system role) both live in `app/services/rbac/registry.py`, wired into `seed_database` after the capability reconcile — all idempotent and edit-preserving. Guard invariants land with the Phase 5 UI. Tests in `tests/test_rbac_seed.py` (6, green). *Adjustment vs plan: the user-role backfill is a startup seeder guarded by "zero existing roles" rather than a one-time data migration — same one-time semantics, but it can run after the capability table is reconciled at startup (a migration couldn't, since capabilities are code-synced at boot).*

Goal: **on upgrade, every existing user behaves exactly as before.**

### 4.1 Seed the four system roles (idempotent, at startup + a data migration)
| Seeded role | `key` | flags | Capabilities granted |
|---|---|---|---|
| **Admin** | `admin` | `is_system`, `is_superuser` | (all — via superuser short-circuit; no rows needed, but seed the full set for display) |
| **Manager** | `manager` | `is_system` | every internal capability *except* the admin-surface set (`lookups.manage`, `library.manage`, `user.*`, `authprovider.*`, `apikey/oauthclient/mcptoken.manage`, `settings.manage`, `feedback.manage`, `region.*`); includes `project.*`, `usecase/note/task.*`, `customer.*`, `grant.manage`, `report.*`, `audit.view`, `note.view_internal` |
| **Standard / SE** | `standard` | `is_system` | same internal set as Manager (they differ only on the **region** axis, not capabilities — confirmed in the catalog: manager vs SE differ in region scoping, not permission gates) |
| **External** | `external` | `is_system`, `is_external` | read-only: `project.view`, `report.generate` (grant-scoped); **not** `note.view_internal`, no edit/create/manage |

> Manager and Standard get identical capability rows on purpose — today they pass the same UI gates and differ only in `access.py` region breadth. Keeping them as two roles preserves the region plan's semantics and lets admins later diverge them.

### 4.2 Backfill `user_roles` (one-time data migration)
For each existing user, resolve their legacy `role` and insert the matching system role:
```
is_admin        -> admin
is_external     -> external
is_manager      -> manager
else            -> standard
```
Exactly the precedence the `role` getter already uses, so the mapping is unambiguous and total. Boolean columns are left as-is (region code still reads them).

### 4.3 Guard invariants preserved (and extended to the new model)
| Invariant (today) | New-model form |
|---|---|
| Can't change your own role (`settings_routes.py:690`) | Can't add/remove **your own** role assignments |
| Seeded admin must stay admin (`:693`) | The seeded admin user must always hold the `admin` (superuser) role |
| Can't demote the last admin (`:697-701`) | ≥1 active user must hold a superuser role (block removing the last one) |
| — (new) | A system role (`is_system`) can't be deleted; its `key`, `is_superuser`, `is_external` flags can't be edited. Manager/SE/External capability *sets* may be edited by admins, but Admin stays superuser. |
| — (new) | A role assigned to any user can't be hard-deleted until unassigned (or reassign-then-delete), same shape as the region delete-guard (`lookup_routes.delete_row`). |

Migrations are `op.create_table` + a data seed/backfill — **no `projects` touch, no `batch_alter_table`**, FTS triggers unaffected.

---

## PHASE 5 — Admin UI (role builder) ✅

> **Done.** New `app/ui/role_routes.py` (prefix `/ui/settings/roles`, every route self-gates on `role.manage`) over a new `app/services/rbac/roles.py` service that holds all guard logic. Surfaces: **roles list** (`settings/roles.html` — counts, system/superuser/external badges, off-switch notice), **role editor** (`settings/role_form.html` — capability matrix grouped by area with per-area "select all"; superuser roles render read-only), and **per-user assignment** (`settings/user_roles.html` — multi-select, external role excluded). Entry points: a Roles tile on the settings index and a "Manage roles" button on the user edit page. Audit events (`role.created/updated/deleted/user_roles_changed`). Guards enforced in the service: no self-role-change, seeded admin keeps a superuser role, never zero superusers, system roles undeletable, custom roles always non-superuser/non-external, and the escalation guard (a non-superuser actor can't grant a superuser role or capabilities they don't hold). Tests in `tests/test_rbac_role_builder.py` (7, green). Verified live in the browser against the real dev DB (roles list + capability matrix render correctly; startup logged 44 caps reconciled, 4 roles seeded, 1 user backfilled).

Gated by the new `role.manage` capability (seeded only into Admin). Lives under Settings, next to Users/Regions.

- **5.1 Roles list** — `/ui/settings/roles`: system roles (badge, locked) + custom roles, with assigned-user counts. Create / edit / delete (delete blocked while assigned or `is_system`).
- **5.2 Role editor** — name, description, and the **capability matrix**: capabilities grouped by `area` (from the registry), checkboxes, "select all in area." System roles render read-only where their flags are locked (Admin's superuser row, External's identity row).
- **5.3 User role assignment** — extend the existing user-edit page (`settings_routes.py:670` area) from a single-select to **multi-select role checkboxes**. Keep the legacy `role` dropdown working during transition (writes both the boolean and the corresponding `user_roles` row) until the switch flips, then retire it. Assigning/removing the External role keeps `is_external` in sync.
- **5.4 Audit** — reuse `_settings_event`: `role.created`, `role.updated`, `role.deleted`, `role.capability_changed`, `user.roles_changed` (mirrors the existing `admin_user.role_changed` event).
- **5.5 Escalation guard in the UI** — a non-superuser with `role.manage` may only grant capabilities they themselves hold, and may never grant `is_superuser` or `role.manage` (see §6).

---

## PHASE 6 — Open questions & risks

1. **Privilege escalation via `role.manage`.** The central risk of *any* dynamic RBAC. A user who can edit roles can grant themselves everything. Mitigations to decide on:
   - Restrict `role.manage` to superusers only in the seed (safe default — recommended for v1), **and/or**
   - Enforce "can't grant a capability you don't hold" + "can't create/edit a superuser role" + "can't grant `role.manage`" at the service layer (not just the UI). Recommend implementing the service-layer guard even if v1 keeps `role.manage` admin-only, so loosening it later is safe.
2. **External as role vs identity.** This plan keeps `is_external` as the identity flag and layers the External *role* on top, kept in sync. Alternative — make External a pure capability bundle — was rejected because it risks the grant/expiry/note-hiding invariants. Confirm this is the intended split, and decide the exact sync rule when an admin assigns a mix of External + internal roles (recommend: **disallow** combining External with any internal role).
3. **Manager vs Standard collapse.** They carry identical capabilities and differ only on the region axis. Options: (a) keep both as distinct system roles (recommended — preserves the region plan and future divergence), (b) merge into one role and let region membership alone distinguish them. If merged, the region plan's manager/SE UI wording needs a pass.
4. **Region-bypass as a capability?** Should "see all regions / bypass region enforcement" be a togglable capability (`region.bypass`) an admin can grant to a custom role, or stay implicit in `is_superuser`? Recommend implicit-in-superuser for v1, with `region.bypass` as a documented future capability — but flag that this is where the two RBAC systems could couple, so decide deliberately.
5. **Capability granularity drift.** ✅ **Decided: keep the full fine-grained splits** (all 44, incl. separate `customer.create`/`edit`/`delete` and the seven `user.*` capabilities). The residual risk is UI noise and a matrix that's tedious to configure — mitigated in the Phase 5 editor by grouping tightly per area with a per-area "select all", and (optional) shipping preset role templates. Merging a split later would be a breaking change, so this is the safe direction to lock in early.
6. **API / MCP surface.** Capabilities govern UI users only; API-key/OAuth principals remain unenforced by role (same boundary as region RBAC). If/when the API should honor capabilities, principals need a capability model of their own (scopes → capabilities). Out of scope here; called out so it isn't assumed covered.
7. **Effective-capability performance & caching.** `can()` is called many times per request. The union query must be memoized per request (or eager-loaded with the user). Low risk on SQLite single-writer, but the caching contract (invalidate on role edit within the same request) should be specified so a mid-request role change can't produce inconsistent checks.
8. **Two master switches.** `rbac_dynamic_enabled` (this plan) and `region_enforcement_enabled` (region plan) are independent. Confirm the intended rollout order — recommend: land + flip dynamic RBAC first (it's behavior-preserving by construction), *then* continue the region Phase 6 rollout, so the two flags are never both mid-flight.

---

## Suggested build order

Foundation first, enforcement last, switch-gated throughout — same philosophy as the region plan:

**Phase 1 (catalog / registry)** → **Phase 2 (tables)** → **Phase 4 (seed + backfill, switch OFF)** → **Phase 3 (repoint call sites in waves, switch OFF)** → parity tests → **Phase 5 (admin UI)** → flip `rbac_dynamic_enabled` in staging → production → delete `_legacy_can`.

Rationale: get the schema seeded and the call sites migrated **while the legacy path still governs**, so the app can't misbehave until the switch is deliberately flipped — and the flip is provably a no-op because the seed reproduces today's four roles exactly.
