# Requirements & Design Decisions

This captures the original requirements and how each is implemented, including the
non-obvious decisions made along the way.

## Framework / Deployment

| Requirement | Implementation |
|---|---|
| Deployable as Docker container(s) | Single container: `Dockerfile` + `docker-compose.yml` (named `poct-data` volume) |
| Stored in a GitHub repo | `github.com/tmanmidwest/POC-Tracker` |
| Runs on Docker Desktop (Mac) for initial testing | Compose maps `POCT_HOST_PORT` (default 8010) |
| REST API to read/edit/update any item | `/api/v1/*` (FastAPI, OpenAPI at `/docs`) |
| MCP server for AI to read & generate reports/queries | `app/mcp_server.py` (`poct-mcp`), read/report tools over the REST API |

## Authentication

| Requirement | Implementation |
|---|---|
| Local authentication initially | Session login against `app_users` (bcrypt) |
| Add an OAuth provider | OIDC SSO via Authlib; providers managed in **Settings → Identity Providers** |
| Admin group (everything) + standard group (add/edit POC projects) | `app_users.is_admin`; admin-only surfaces gated by `require_admin_ui` |
| API keys separate from OAuth | **Settings → API Keys**; bearer `poct_…` tokens for the REST API & MCP |

**Decision — Account Executives are not users (phase 1).** AEs are tracked as reference
fields on a project (`account_executive`, `account_executive_email`); they don't log in.
Sales Engineers *are* app users since they edit POCs.

## Project Requirements

| Requirement | Implementation |
|---|---|
| Prospect / Customer name | `customers.name` |
| Contacts: name, email, phone, role | `contacts`; role FK → `contact_roles` master list |
| Role from a master list | `contact_roles` (Champion, Sourcing, Technical/Business Stakeholder, …) |
| Project status from a global list | `projects.status_id` → `project_statuses` (with sort order for the dashboard) |
| Start & end dates | `projects.start_date` / `end_date` |
| Assign a Sales Engineer (user) | `projects.sales_engineer_id` → `app_users` |
| Assign an Account Executive | `projects.account_executive*` (reference only) |

## Use Cases

| Requirement | Implementation |
|---|---|
| Master library to pick from | `use_case_library` (category, name, description, success validation, feature type) |
| Feature/platform type from a global list | `feature_types` (JML, ISPM, Certifications, NHI, AI, …) |
| Add ad-hoc customer use cases | `project_use_cases.source = "custom"` |
| Reference number for listing/sorting (1.1, 3.4) | `project_use_cases.reference_number` (per-project, sorted numerically by segment) |
| Status per use case | `project_use_cases.status_id` → `use_case_statuses` |
| Comments per use case | `project_use_cases.comments` |
| Screenshot upload(s) once completed | `screenshots` (files on the data volume, multiple per use case) |

**Decision — use cases are copy-on-add snapshots, not links.** When you pick library
entries for a project, each is *copied* into `project_use_cases` (with `library_id`
recorded for provenance). Editing or deleting the library afterwards never mutates a POC
already in flight. The picker shows already-added entries as checked/disabled, and adding
is de-duplicated by `library_id`, so re-opening the picker never creates duplicates.

**Decision — reference numbers are per-project.** A project uses only a subset of the
library and may renumber to read cleanly, so the number lives on the project copy
(seeded from the library's `default_reference_number`, then freely editable).

## Dashboard

| Requirement | Implementation |
|---|---|
| Login lands on a dashboard sorted by project status | `/ui/dashboard`, grouped by `project_statuses.sort_order` |
| User-editable: which fields show, which statuses, sort | `dashboard_prefs` (per-user JSON), edited at **Dashboard → Customize** |

## Reporting

| Requirement | Implementation |
|---|---|
| Report of all POCs | `/ui/reports` (print-friendly) |
| Report of a single POC with all info | `/ui/reports/projects/{id}` (and the MCP `project_report` tool) |

**Decision — standard users have shared edit on all projects.** Requirement 10 grants
standard users add/edit on POC projects with no ownership qualifier, and the SE/AE are
already tracked per project, so there is no per-row ownership authz. Admin-only actions
are limited to managing lookups, the library, users, identity providers, and settings.

**Decision — screenshots stored on the data volume**, not as DB blobs, matching the
named-volume model and keeping the DB/backups small.

## Task Manager

A per-user task manager alongside projects. Full details in [TASKS.md](TASKS.md).

| Requirement | Implementation |
|---|---|
| Users manage their own tasks (not global) | `tasks.owner_user_id` → app_users; every task surface scopes to the owner (admins can view all; external viewers none) |
| Statuses managed at the admin level, applied to all users' tasks | `task_statuses` lookup (**Settings → Lookups**), global |
| Title, status, priority, start & end dates (dates optional) | `tasks.title` / `status_id` / `priority_id` (optional) / `start_date` / `due_date` |
| Priority | `task_priorities` lookup (admin-managed, with a badge `color`) |
| Rich-text details like journal entries | `tasks.details` / `details_html` (same Quill + `nh3` sanitizer as project notes) |
| Assign tasks to projects | `tasks.project_id` (optional single project) |
| A dashboard like the project one, with filters/views | `/ui/tasks` grouped by status; per-user `task_dashboard_prefs` |
| Project page shows its assigned tasks | Tasks card on the project detail page (viewer's own; admins see all) |
| Sync a task to the user's Google Tasks (phase 2) | Per-user two-way Google Tasks sync — see below |

**Decision — tasks are per-user, statuses/priorities are global.** Ownership lives on the
task (`owner_user_id`); the pickable lists are admin-managed and shared, mirroring how the
rest of the app splits admin-controlled config from user-owned data.

**Decision — optional single project link, `SET NULL` on delete.** A task belongs to at most
one project (or none, for a personal to-do). Deleting a project doesn't delete its tasks —
their `project_id` is nulled, so they survive unassigned.

**Decision — priorities are an admin-managed lookup, not a fixed enum.** Same treatment as
statuses, so admins can rename/recolor/extend the levels.

**Decision — REST/MCP are admin-wide with an explicit `owner`.** Those interfaces authenticate
as a machine identity (API key / OAuth client), not a logged-in user, so `create`/`update`
take an explicit `owner` (username or id). A later phase could bind a key to a user and default
the owner from it.

## Google Tasks sync (phase 2)

Optional, per-user, two-way sync between a user's tasks and **their own** Google Tasks account.
Full setup and semantics in [TASKS.md](TASKS.md).

| Requirement | Implementation |
|---|---|
| Per-user integration to their own calendar/tasks | One admin-registered OAuth **client** (`google_tasks_config`); each user connects their own account (`user_google_credentials`, encrypted refresh token) |
| Creating a task in POC Tracker creates it in Google | Best-effort **push** on every task change (`google_tasks_sync`), into a dedicated "POC Tracker" list |

**Decision — Google Tasks, not Calendar events.** A to-do maps naturally to Google Tasks
(title/notes/due/complete); Calendar events would force a time-of-day model. Trade-off:
start_date, priority, and project have no Google Tasks equivalent and don't round-trip.

**Decision — two-way, with explicit conflict rules.** Last-edit-wins by timestamp; a Google
delete **archives** the POC task (reversible); a task created in the Google list is **imported**;
a terminal status ↔ `completed`; Google note edits flatten `details` to plain text.

**Decision — one OAuth client, per-user consent.** The admin registers a single OAuth app; users
consent individually — the standard model (like "Sign in with Google"), not a shared account and
not a client per user. Because users bring external Google accounts, the consent screen is
**External** (with Google's test-user limit / weekly re-consent until the app is published +
verified — handled by a **Reconnect** prompt).

**Decision — push first, pull second.** Increment 1 (shipped) is push (POC → Google) plus all
the OAuth/token plumbing; increment 2 (planned) is the reconcile poll that pulls Google-side
changes back. Built and tested against a mocked Google backend; a live Google project is only
needed to actually sync.
