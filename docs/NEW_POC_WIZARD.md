# New POC Wizard & POC Templates

**Status:** Phase 1 & Phase 2 implemented (shipped in app version 0.4.0)
**Author:** (design notes)
**Related:** `docs/SCHEMA.md`, `docs/TASKS.md`, `app/services/use_cases.py`, `app/services/library_sets.py`

> **Phase 1 shipped.** Wizard at `GET/POST /ui/projects/wizard`, orchestrated by
> `app/services/poc_wizard.py` (single atomic commit), rendered by
> `app/templates/projects/wizard.html`, with a "New POC" nav item under Dashboard
> (`app/templates/base.html`). Covered by `tests/test_poc_wizard.py`. Tasks **were
> included** in Phase 1 (resolves open question O1).
>
> **Phase 2 shipped.** POC Templates: models `PocTemplate` / `PocTemplateUseCase` /
> `PocTemplateTask` (migration `0027_add_poc_templates`), service
> `app/services/poc_templates.py`, management pages at `/ui/templates`
> (`app/templates/templates/`), "Save as template" on the project detail page, and a
> template picker on the wizard (`GET /ui/projects/wizard?template_id=N` pre-fills
> the flow). "POC Templates" nav item under Use Case Library. Covered by
> `tests/test_poc_templates.py`. Authoring in v1 is snapshot-only ("Save this POC as
> a template"); hand-editing a template's contents is a follow-up (see below).

---

## 1. Problem

Creating a POC today is a multi-screen, multi-transaction chore:

1. Create the customer (`POST /ui/customers/new`)
2. Create the project (`POST /ui/projects/new`) — flashes *"Project created. Now add use cases."*
3. Land on the project detail page
4. Add use cases (from library picker **or** custom form)
5. Add tasks / notes

Each step is a separate page and a separate commit. A new sales engineer has to *know* this sequence, and there's nothing that teaches "what a good POC looks like." Reps rebuild the same standard structure from scratch every time.

Two complementary ideas solve this:

- **The Wizard** — a single guided flow that collapses steps 1–5 into one path, with inline explainers. Solves the *"where do I click next"* friction.
- **POC Templates** — reusable, pre-filled blueprints (use cases + tasks + defaults) selectable at the start of the wizard. Solves the *"what do I even put here"* friction.

They ship in that order. The wizard is valuable on its own; templates layer into it later.

---

## 2. Scope & phasing

| Phase | Deliverable | New tables? | Risk |
|-------|-------------|-------------|------|
| **1** | New POC Wizard (blank start) | **No** | Low — pure orchestration over existing services |
| **2** | POC Templates ("start from template" on step 0) | Yes (`poc_template`, `poc_template_use_case`, `poc_template_task`) | Medium — new model + admin CRUD |

This doc specs **Phase 1 in detail** and sketches **Phase 2** enough to not paint us into a corner.

Non-goals:

- The wizard does **not** replace the existing individual create screens. `+ New Project`, `+ New Customer`, and the detail-page use-case/task forms all remain as the power-user quick path.
- No change to the underlying models in Phase 1.

---

## 3. Key architectural decision — draft accumulation, single atomic commit

Today every object is created in its own request + commit, and there is **no draft concept** in the data model.

**Chosen approach: client-accumulated draft → one bundled commit.**

The wizard holds all state in the browser (JS) across steps and creates **nothing** in the database until the final **"Create POC"** click. That click POSTs to a single new endpoint which performs customer → project → use cases → (tasks) inside **one DB transaction**, reusing the existing service functions.

Why:

- **No orphans.** If the user bails at step 3, nothing was written. "Cancel" is clean.
- **No schema changes.** No `is_draft` flags to filter out of every list/report/count.
- **No cleanup job** for abandoned drafts.
- **Reuses existing logic.** The bundled endpoint orchestrates services that already exist (`copy_library_entries_to_project`, project/customer/task creation), rather than duplicating creation logic.

**Rejected alternative:** persisting real "draft" rows with a status flag. Requires a migration, an abandoned-draft cleanup path, and draft-filtering in every query that lists or counts projects/customers. Not worth it for v1.

---

## 4. Entry point

A dedicated **"New POC"** item in the sidebar nav, placed **directly under Dashboard** (internal users only).

Location: `app/templates/base.html`, immediately after the Dashboard link (currently ends at line 53), before the Projects link (line 55). Follows the existing `sidebar__link` + inline-SVG pattern. Gate with `{% if current_user and current_user.is_internal %}` since POC creation is an internal action.

Route: `GET /ui/projects/wizard` renders the wizard shell; `active_section = 'new_poc'` for nav highlighting.

The existing `+ New Project` button on the Projects list stays as the quick path.

---

## 5. Wizard flow (Phase 1)

Single page, client-side step navigation (no full reloads between steps). State accumulates in JS; nothing is written until final submit. HTMX (already loaded) is used for in-step server lookups (customer search, library picker) but **not** for creating rows mid-flow.

Each step carries a one-line **explainer** (the teaching layer). Explainers are per-step one-liners, not modal walls of text, so they don't nag repeat users.

### Step 0 — Start
- Phase 1: just a "Blank POC" start (and, when Phase 2 lands, a template picker here).
- Explainer: *"Set up a customer, project, and use cases in one flow."*

### Step 1 — Customer  *(new-customer-first)*
- **Default: create a new customer** — name (required), website, notes.
- **Escape hatch at top:** a small *"This POC is for an existing customer"* link → swaps to a customer search/select (reuses existing customer lookup). Chosen because most POCs start with a new customer, but the occasional existing one shouldn't force a hunt.
- Explainer: *"Who is this POC for? Most POCs start with a new customer."*

### Step 2 — Project details
- Fields mirror `POST /ui/projects/new`: name (optional label), status (defaults via `default_project_status_id`), start/end date, sales engineer, account executive + email, Salesforce / notebook / POC instance URLs, notes.
- Explainer: *"The basics of the engagement. You can refine dates and links later."*

### Step 3 — Use cases
- Reuses the library picker UX (grouped by set → category) plus an "add custom use case" affordance.
- Template (Phase 2) pre-checks a set; user adds/removes freely.
- Explainer: *"Pick the use cases you'll validate. Start from the library or add your own."*

### Step 4 — Tasks  *(optional — see open question O1)*
- Seeded from template (Phase 2). Relative dates ("kickoff + 3 days") resolve against project start.
- Skippable.
- Explainer: *"Optional: kickoff tasks to track. You can add these anytime."*

### Step 5 — Review & create
- Summary of everything to be created.
- Single **"Create POC"** button → bundled commit → redirect to the new project detail page.
- Explainer: *"Review and create. Nothing is saved until you click Create POC."*

**Escape hatch:** a persistent *"Create now"* action lets a power user commit with only the minimum filled (customer + project), so the wizard is never slower than the manual path.

---

## 6. Backend — the bundled endpoint

New route (Phase 1):

```
POST /ui/projects/wizard
```

Accepts one payload describing the whole POC:

- `customer`: either `{ new: {name, website, notes} }` or `{ existing_id: <int> }`
- `project`: the `POST /ui/projects/new` field set
- `use_cases`: `{ library_ids: [...], custom: [{category, name, ...}] }`
- `tasks` (optional): `[{title, status_id, start_offset_days, due_offset_days, ...}]`

Handler logic, all inside **one transaction**:

1. Resolve/create customer.
2. Create project (default status via `default_project_status_id(db)`).
3. `copy_library_entries_to_project(db, project, library_ids)` for library use cases; create `ProjectUseCase(source=custom)` rows for custom ones (default status via `default_use_case_status_id`).
4. Create tasks (owner = current user), resolving relative offsets against `project.start_date`.
5. Commit once. On error, roll back the whole thing.

Audit: emit the existing per-entity events (`customer.created`, `project.created`, `use_case.added_from_library`, etc.) within the transaction, plus a wrapping `poc.created_via_wizard` event for traceability.

Reuse, don't duplicate: this handler calls the same service functions the individual routes call. The individual routes are unchanged.

---

## 7. Phase 2 — POC Templates (sketch)

A template can't piggyback on library sets: **library sets cover use cases but not tasks or project defaults.** So templates need their own storage.

Proposed tables:

- `poc_template` — `id`, `name`, `description`, `is_active`, default project fields (status, default duration in days, default SE?), `created_by`, timestamps.
- `poc_template_use_case` — links a template to library entries and/or carries custom use-case snapshots.
- `poc_template_task` — task blueprints with **relative** dates (`start_offset_days`, `due_offset_days`) resolved against project start at apply time.

Integration points:

- **Step 0** of the wizard gains a template picker; selecting one pre-fills steps 2–4 (all still editable).
- **Authoring:** admin CRUD under Settings, and/or a **"Save this POC as a template"** action on an existing project detail page that snapshots its structure (stripping customer-specific data).
- **Apply-time only** (v1): templates seed a new POC; they do not retro-sync into existing POCs when the template changes.

Open: global (org-wide) vs. personal templates; admin-only vs. anyone authoring. Defer until Phase 1 ships and we see real usage.

---

## 8. Open questions

- **O1 — Tasks in the Phase 1 wizard, or defer to Phase 2?** Tasks add a step and the relative-date resolution logic. Could ship the wizard as Customer + Project + Use Cases first, add the Tasks step alongside templates.
- **O2 — Explainer style:** per-step one-liners (current lean) vs. a dismissible "first time?" intro panel.
- **O3 — Permissions:** any internal user creates POCs via the wizard (matches today's create permissions) — confirm no tighter gate wanted.
- **O4 — Wizard state persistence:** if the user navigates away mid-wizard, is losing the in-progress draft acceptable (simplest), or do we want to stash it in `sessionStorage` so a refresh doesn't wipe it?

---

## 9. Rough file touch-list (Phase 1)

| Area | File(s) | Change |
|------|---------|--------|
| Nav entry | `app/templates/base.html` (~line 53) | Add "New POC" `sidebar__link` under Dashboard, internal-only |
| Wizard page | `app/templates/projects/wizard.html` (new) | Multi-step shell + client JS |
| Routes | `app/ui/project_routes.py` | `GET /ui/projects/wizard`, `POST /ui/projects/wizard` |
| Orchestration | `app/services/` (reuse existing; maybe a thin `wizard.py` orchestrator) | Bundled create in one transaction |
| Nav highlight | wherever `active_section` is set | Add `'new_poc'` |

No model or migration changes in Phase 1.
