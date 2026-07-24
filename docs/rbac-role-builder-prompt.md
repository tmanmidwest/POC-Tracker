# RBAC role-builder — new-session kickoff prompt

Paste the block below into a fresh session to start scoping the dynamic RBAC
"role builder." It's a DESIGN/SCOPING task first — not implementation. The
resulting plan should be written to `docs/rbac-role-builder-plan.md` (mirroring
`docs/rbac-region-plan.md`).

---

```
I want to scope out a dynamic RBAC "role builder" for Questlog (the POC-Tracker
app in this repo) — admin-defined roles with configurable permissions, replacing
today's four hardcoded roles. This session is for DESIGN/SCOPING ONLY. Do not
write implementation code yet. The deliverable I want is a written scoping doc
(propose saving it as docs/rbac-role-builder-plan.md, mirroring the style of the
existing docs/rbac-region-plan.md).

Start by reading these to ground yourself:
- app/models/app_user.py — the current role model
- docs/rbac-region-plan.md — the in-flight region RBAC work
- app/services/access.py, app/ui/dependencies.py — where authorization is enforced

Current state you should confirm by reading, not assume:
- There are 4 hardcoded, mutually-exclusive roles: admin, manager, standard
  (labeled "SE" in the UI), external. Stored as independent booleans (is_admin,
  is_external, is_manager) and resolved via the AppUser.role property/setter.
- Roughly 70 authorization call sites across the app key off these roles/booleans
  (grep for `.role ==`, `is_admin`, `is_manager`, `is_external`, `ROLE_`).
- A separate Region-based RBAC effort is Phases 0–5 done but NOT finished: Phase 6
  (access-control tests, migration test on a prod DB copy, staged rollout) is open,
  and the master switch `region_enforcement_enabled` still defaults OFF. The role
  builder must not disrupt or duplicate that work.

Constraints to design around (verify against the repo/CLAUDE.md/memory):
- SQLite database, single-writer (one ECS task). Migrations run at startup.
- Migrations touching the `projects` table need the batch_alter_table / FTS-trigger
  handling used by existing migrations — check how prior migrations did it.
- Backward compatibility is required: today's 4 roles must map cleanly onto seeded
  roles in the new model so existing users/behavior don't change on upgrade.
- Preserve existing guard invariants (e.g. can't demote the last admin, the seeded
  admin stays admin, no self-role-change).

I want the scoping doc to work in this order (this was the recommended sequence):
1. A CAPABILITY CATALOG first — the concrete list of permissions/actions that must
   be able to vary independently (derive these from the ~70 existing enforcement
   sites, so we know what granularity is actually needed). This is the foundation;
   everything else depends on it.
2. A data model — roles table, permissions/capabilities, a role↔permission map, and
   user↔role assignment. Note how it coexists with the region model.
3. An enforcement layer — a single helper like `user.can("capability")` to replace
   scattered role checks, and a migration path for the ~70 call sites.
4. A migration mapping the 4 current roles → seeded default roles.
5. A guarded admin UI for building/editing roles.
6. Open questions and risks (e.g. multiple-roles-per-user vs single, how region
   scoping composes with capabilities, protecting against privilege escalation).

Ask me clarifying questions before finalizing the plan — especially about the
actual permission granularity we need and whether a user can hold more than one
role. Don't guess the requirements; the whole point of scoping first is to pin
those down.
```
