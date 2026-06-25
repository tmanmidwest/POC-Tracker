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
          └──< project_use_cases ──> use_case_statuses (status_id)
                   │            └───> feature_types     (feature_type_id)
                   │            └┄┄┄> use_case_library  (library_id, provenance; SET NULL on delete)
                   └──< screenshots

use_case_library ──> feature_types
dashboard_prefs ──> app_users (one row per user)
```

### Core tables

- **customers** — `name` (unique), `website`, `notes`. Cascades to contacts.
- **contacts** — `customer_id`, `name`, `email`, `phone`, `role_id` → contact_roles.
- **projects** — `customer_id`, `name` (optional; falls back to customer name),
  `status_id`, `start_date`, `end_date`, `sales_engineer_id` → app_users,
  `account_executive` / `account_executive_email` (reference only), `notes`,
  `is_archived` / `archived_at`.
- **project_use_cases** — a use case attached to a project:
  - `source` — `"library"` (a snapshot of a library entry) or `"custom"` (ad-hoc).
  - `library_id` — provenance only; `SET NULL` if the library entry is deleted.
  - `reference_number` (per-project), `category`, `name`, `description`,
    `success_validation`, `feature_type_id`, `status_id`, `comments`.
- **use_case_library** — master template: `category`, `default_reference_number`,
  `name`, `description`, `success_validation`, `feature_type_id`, `is_active`.
- **screenshots** — `project_use_case_id`, `stored_filename` (on `<data_dir>/screenshots`),
  `original_filename`, `content_type`, `size_bytes`, `caption`.
- **dashboard_prefs** — `app_user_id` (unique), `config_json` (columns, statuses, sort).

### Lookups (admin-managed, `is_active` + `is_system`)

- **contact_roles** — `name`.
- **project_statuses** — `name`, `sort_order` (dashboard grouping), `is_terminal`.
- **feature_types** — `name`, `description`.
- **use_case_statuses** — `name`, `sort_order`, `is_complete_status` (drives progress %).

`is_system` rows are seed defaults and cannot be deleted; a lookup still referenced by
live data cannot be deleted either (deactivate it instead).

## Platform tables (shared scaffold)

`app_users` (with `is_admin`), `api_keys`, `oauth_clients`, `auth_providers`,
`user_identities`, `app_branding`, `app_config`, `audit_events`.
