# Data Model

SQLite via SQLAlchemy 2.0; migrations in `alembic/`. All tables carry `created_at` /
`updated_at` (see `TimestampMixin`).

## Domain

```
customers ──< contacts ───────> contact_roles
   │                              (master list)
   └──< projects ──> project_statuses (status_id)
          │     └──> app_users        (sales_engineer_id)
          │
          ├──< project_use_cases ──> use_case_statuses (status_id)
          │        │            └───> feature_types     (feature_type_id)
          │        │            └┄┄┄> use_case_library  (library_id, provenance; SET NULL on delete)
          │        └──< screenshots
          └──< project_notes ──< note_attachments

use_case_library ──> feature_types
dashboard_prefs ──> app_users (one row per user)
projects ──< project_grants ──> app_users (per-project read access for external viewers)

tasks ──> task_statuses (status_id)
  │   └─> task_priorities (priority_id, nullable)
  │   └─> app_users       (owner_user_id; per-user ownership)
  │   └┄> projects        (project_id, nullable; SET NULL on project delete)
task_dashboard_prefs ──> app_users (one row per user)
user_google_credentials ──> app_users (one row per connected user)
google_tasks_config (singleton, id=1)
```

### Core tables

- **customers** — `name` (unique), `website`, `notes`. Cascades to contacts.
- **contacts** — `customer_id`, `name`, `email`, `phone`, `role_id` → contact_roles.
- **projects** — `customer_id`, `name` (optional; falls back to customer name),
  `status_id`, `start_date`, `end_date`, `sales_engineer_id` → app_users,
  `account_executive` / `account_executive_email` (reference only), `notes` / `notes_html`,
  `is_archived` / `archived_at`. AI executive summary: `exec_summary` (plain text),
  `exec_summary_html` (editable rendering), `exec_summary_generated_at`,
  `exec_summary_model` (e.g. `anthropic/claude-opus-4-8`), `exec_summary_tokens` (tokens used).
- **project_use_cases** — a use case attached to a project:
  - `source` — `"library"` (a snapshot of a library entry) or `"custom"` (ad-hoc).
  - `library_id` — provenance only; `SET NULL` if the library entry is deleted.
  - `reference_number` (per-project), `category`, `name`, `description`,
    `success_validation`, `feature_type_id`, `status_id`, `comments`.
- **use_case_library** — master template: `category`, `default_reference_number`,
  `name`, `description`, `success_validation`, `feature_type_id`, `is_active`.
- **screenshots** — `project_use_case_id`, `stored_filename` (on `<data_dir>/screenshots`),
  `original_filename`, `content_type`, `size_bytes`, `caption`.
- **project_notes** — a dated journal entry on a project: `project_id` (`ON DELETE CASCADE`),
  `note_date` (user-facing, editable), `body` (plain text for search/export) / `body_html`
  (sanitized rich text), `created_by`, `is_internal_only` (when true, hidden from external
  viewers; default false). Cascades to note_attachments.
- **note_attachments** — files attached to a note: `note_id` (`ON DELETE CASCADE`),
  `stored_filename` (on the data volume), `original_filename`, `content_type`, `size_bytes`.
- **dashboard_prefs** — `app_user_id` (unique), `config_json` (columns, statuses, sort).

### Tasks (per-user)

- **tasks** — a user-owned task: `owner_user_id` → app_users (`ON DELETE CASCADE`),
  `title`, `status_id` → task_statuses, `priority_id` → task_priorities (nullable),
  `project_id` → projects (nullable, `ON DELETE SET NULL`), `start_date`, `due_date`
  (both nullable), `details` / `details_html` (rich text, same dual-storage as notes),
  `is_archived` / `archived_at`, `is_internal_only` (default false; when true, hidden from external
  viewers on a shared project's Tasks card — see `services/tasks.visible_project_tasks`). Google-sync columns
  (reserved in 0021, used by the sync): `sync_enabled`, `external_id` (Google task id),
  `external_etag`, `last_synced_at`.
- **task_dashboard_prefs** — `app_user_id` (unique), `config_json` (columns, statuses,
  priorities, sort, owner scope, show-archived). Separate from `dashboard_prefs` so task
  and project views don't clobber each other.

### Google Tasks sync

- **google_tasks_config** — singleton (`id=1`): the app's Google OAuth **client**
  credentials — `client_id`, `client_secret_encrypted` (Fernet, recoverable), `is_enabled`.
- **user_google_credentials** — one row per connected user: `app_user_id` (unique,
  `ON DELETE CASCADE`), `refresh_token_encrypted` (Fernet), `scopes`, `google_email`,
  `tasklist_id` (their dedicated "POC Tracker" list), `status` (`connected` /
  `needs_reauth`), `connected_at`, `last_sync_at` (pull high-water mark), `last_error`.

### Email (SMTP)

- **smtp_config** — singleton (`id=1`): outbound mail server settings —
  `host`, `port` (default 587), `security` (`none` / `starttls` / `ssl`),
  `username`, `password_encrypted` (Fernet, recoverable), `from_email`, `from_name`,
  `is_enabled`. Admin-managed under **Settings → Email**; used to send external-user
  invitations (see [INVITATIONS.md](INVITATIONS.md)).

### Lookups (admin-managed, `is_active` + `is_system`)

- **contact_roles** — `name`.
- **project_statuses** — `name`, `sort_order` (dashboard grouping), `is_terminal`.
- **feature_types** — `name`, `description`.
- **use_case_statuses** — `name`, `sort_order`, `is_complete_status` (drives progress %).
- **task_statuses** — `name`, `sort_order` (dashboard grouping), `is_terminal` (→ Google `completed`).
- **task_priorities** — `name`, `sort_order`, `color` (hex badge).

`is_system` rows are seed defaults and cannot be deleted; a lookup still referenced by
live data cannot be deleted either (deactivate it instead).

### Access control

- **project_grants** — grants one external viewer read access to one project:
  `project_id` → projects (`ON DELETE CASCADE`), `user_id` → app_users (`ON DELETE CASCADE`),
  `tier` (default `"viewer"`), `granted_by_user_id`. Unique on `(project_id, user_id)`.
  External viewers see only the projects they're granted. Admins always see every project and
  ignore grants. Standard users (SEs) and managers ignore grants too, but their project
  visibility depends on **region enforcement** (below): off = they see all projects; on = they
  see only projects in their regions. Enforced in the web UI.
- **regions** — admin-managed lookup of geographic regions (e.g. AMER, EMEA, APAC):
  `name` (unique), `sort_order`, `description`, `is_active`, `is_system` (the seeded
  "Unassigned" fallback bucket, undeletable). The axis for region-based access control.
- **user_regions** — many-to-many membership linking app_users ↔ regions:
  `user_id` → app_users (`ON DELETE CASCADE`), `region_id` → regions (`ON DELETE CASCADE`),
  unique on `(user_id, region_id)`. A standard SE has one row (their home region); a manager
  has several. Admins and external viewers ignore it.
- **Region enforcement** is gated by `app_config.region_enforcement_enabled` (default **false**;
  toggle in **Settings → System**). When off, every internal user sees all projects (historical
  behavior). When on, a standard SE / manager is scoped to `projects.region_id ∈` their
  `user_regions` (plus any project where they're the assigned SE); a project with no region is
  visible to admins only. Each project's `region_id` is derived from its assigned SE's region;
  the **backfill** action (Settings → System) region-tags existing projects. The single choke
  points are `services/access.accessible_project_ids` / `can_view_project` / `can_edit_project`
  and `services/scope.scoped_project_ids`.
- **user_invites** — one external-user invitation: `user_id` → app_users (`ON DELETE CASCADE`),
  `project_id` → projects (`ON DELETE SET NULL`, the project it was about), `email` / `company` /
  `invited_name` (snapshots), `token_hash` (SHA-256 of the emailed single-use token — plaintext
  never stored), `status` (`pending` / `accepted` / `revoked`), `expires_at`, `accepted_at`,
  `invited_by_user_id`. Accepting sets the user's password and activates the account. See
  [INVITATIONS.md](INVITATIONS.md).
- **Note & task visibility.** Within a shared project, an individual journal note or task can be
  flagged `is_internal_only`. Two server-side choke points filter those out for external viewers:
  `services/access.visible_project_notes()` (project page, on-screen report, PDF/DOCX, artifacts
  zip) and `services/tasks.visible_project_tasks()` (the project page's Tasks card, read-only for
  viewers). Internal users always see everything. Filtering happens before render, so internal-only
  content never reaches an external client.

## AI

- **ai_providers** — a configured text-generation provider for AI features (executive
  summaries, the requirements importer): `provider` (registry key, e.g. `"anthropic"`),
  `display_name`, `model`, `api_key_encrypted` (Fernet, recoverable), `is_enabled`,
  `is_default` (the one used for generation), `created_by_user_id`, `last_used_at`.

## Platform tables (shared scaffold)

`app_users` (with `is_admin`, `is_external`, `is_manager` — the role flags, resolved by the
`AppUser.role` property into `admin` / `manager` / `standard` / `external` — and, for invited
external users, a unique `email` used as their login id plus `company`), `api_keys`,
`oauth_clients`, `auth_providers` (with `default_user_tier` — the tier given to users it
provisions), `user_identities`, `app_branding`, `app_config` (includes
`region_enforcement_enabled`), `audit_events`.
