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
