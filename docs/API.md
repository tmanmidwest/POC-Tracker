# REST API

Base path `/api/v1`. Interactive docs at `/docs`, OpenAPI at `/openapi.json`.

## Authentication

The API has **two** auth modes depending on the endpoint:

1. **Bearer token** — for the data/resource endpoints (customers, projects, use
   cases, library, tasks, lookups). Use an **API key** (Settings → API Keys,
   `poct_…`) or an **OAuth access token** from the client-credentials flow.

   ```
   Authorization: Bearer poct_xxxxxxxx
   ```

2. **Session cookie** (admin UI login) — for the **credential-management**
   endpoints under `/api/v1/auth/*` (managing API keys and OAuth clients, and the
   session login/logout/me calls). These are the same actions available in
   Settings and are **not** reachable with a bearer token; they authenticate with
   the logged-in admin session. This bootstraps credentials (you log in, then mint
   an API key) without a chicken-and-egg problem.

OAuth token endpoint (RFC 6749 client_credentials):

```bash
curl -X POST http://localhost:8010/oauth/token \
  -d grant_type=client_credentials -d client_id=poct_client_… -d client_secret=…
```

## Endpoints

### Lookups
- `GET|POST /contact-roles/`, `GET|PATCH|DELETE /contact-roles/{id}`
- `GET|POST /project-statuses/`, `…/{id}`
- `GET|POST /feature-types/`, `…/{id}`
- `GET|POST /use-case-statuses/`, `…/{id}`
- `GET|POST /task-statuses/`, `…/{id}` — `name`, `sort_order`, `is_terminal`
- `GET|POST /task-priorities/`, `…/{id}` — `name`, `sort_order`, `color`

List accepts `is_active`. `is_system` rows and lookups still in use cannot be deleted (409).

### Customers & contacts
- `GET|POST /customers/`, `GET|PATCH|DELETE /customers/{id}` (detail includes contacts)
- `GET|POST /customers/{id}/contacts`, `PATCH|DELETE /customers/contacts/{contact_id}`

### Library sets
Named containers for library entries (a project uses a set's entries as snapshots).
- `GET|POST /library-sets/`, `GET|PATCH|DELETE /library-sets/{id}`
- Create/update body: `name`, `description`, `is_active`. List filter: `is_active`.
- Delete is **409** if the set still contains entries, or if it's the default set.

### Use-case library
- `GET|POST /use-case-library/`, `GET|PATCH|DELETE /use-case-library/{id}`
- List filters: `is_active`, `category`, `library_set_id`.
- Entries belong to a library set (`library_set_id`) and carry `feature_type_id`.

### Projects & use cases
- `GET|POST /projects/`, `GET|PATCH|DELETE /projects/{id}` (detail includes use cases)
- `GET /projects/{id}/use-cases`
- `POST /projects/{id}/use-cases` — add an ad-hoc (custom) use case
- `POST /projects/{id}/use-cases/from-library` — `{"library_ids": [...]}`, copies as
  snapshots (de-duplicated)
- `PATCH|DELETE /projects/use-cases/{use_case_id}`
- Use-case responses include their **screenshots** (read-only); uploading/deleting
  screenshots is UI-only (see "Not exposed" below).

### Project notes (dated journal entries)
- `GET /projects/{id}/notes` — a project's notes, newest first
- `POST /projects/{id}/notes` — body: `body` (HTML, sanitized; required), `note_date`
  (defaults to today), `is_internal_only` (bool, default false), `created_by`
  (display label; defaults to the calling principal)
- `GET /projects/notes/{note_id}`
- `PATCH /projects/notes/{note_id}` — any of `body`, `note_date`, `is_internal_only`
- `DELETE /projects/notes/{note_id}`

Responses include read-only **attachment** metadata; uploading note attachments is
UI-only. `is_internal_only` hides the note from external viewers (see INVITATIONS.md).
Notes are also available over **MCP** (`list_notes`, `get_note`, `add_note`,
`update_note`, `delete_note`), and `project_report` includes them.

### Tasks (per-user; admin-wide over the API)

Tasks are owned by a user, but the API authenticates as a machine, so these
operate across all users and take an explicit **`owner`** (username or id).

- `GET /tasks/` — filters: `owner`, `status_id`, `priority_id`, `project_id`, `include_archived`
- `GET /tasks/{id}`
- `POST /tasks/` — body: `owner` (required), `title` (required), `status`, `priority`
  (name or id), `project_id`, `start_date`, `due_date`, `details` (HTML, sanitized),
  `is_internal_only` (bool, default false — hides the task from external viewers)
- `PATCH /tasks/{id}` — any of the above; `owner` reassigns; `is_archived` archives;
  `is_internal_only` toggles viewer visibility
- `DELETE /tasks/{id}`

All `/tasks/*` return **404** when the Task Manager module is disabled. See
[TASKS.md](TASKS.md).

### Auth & credential management (session-authenticated)

These live under `/api/v1/auth/*` and use the **admin session cookie**, not a
bearer token. They mirror Settings → API Keys / OAuth Clients.

- **Session:** `POST /auth/session/login` (`username`, `password`),
  `POST /auth/session/logout`, `GET /auth/session/me`
  (`authenticated`, `username?`, `user_id?`).
- **API keys:** `GET /auth/api-keys/`, `POST /auth/api-keys/`
  (`name`, optional `expires_at` — the full key is returned **once**),
  `GET /auth/api-keys/{id}`, `POST /auth/api-keys/{id}/revoke`,
  `DELETE /auth/api-keys/{id}`. No PATCH — keys are immutable.
- **OAuth clients:** `GET /auth/oauth-clients/`, `POST /auth/oauth-clients/`
  (`name`, `token_lifetime_seconds` 60–86400 — the secret is returned **once**),
  `GET /auth/oauth-clients/{pk}`, `POST /auth/oauth-clients/{pk}/revoke`,
  `DELETE /auth/oauth-clients/{pk}`. No PATCH.

## Not exposed via the API

By design, these product features are **UI-only** (and, where noted, available
over MCP) — there is no REST endpoint today:

- **Project content:** note **attachments** and use-case **screenshots** (readable
  nested, but no upload/delete), and AI **executive summaries** (generate/read/edit).
  (Journal **notes** themselves *are* exposed — see Project notes above.)
- **Sharing & external users:** project **grants**, **invitations**, and **user**
  management (`app_users`). These stay UI-gated (email + access-control workflows).
- **Admin/config:** identity providers (OIDC), SMTP, AI providers, Google Tasks
  config, branding, system config, backups, MCP gateway tokens — all hold secrets
  or are operational, so they're admin-UI only.
- **Read/utility:** the **audit/activity** log, **search**, per-user dashboard
  **preferences**, and **reports** (PDF/DOCX/zip). Reports and read-only project
  data (including journal notes) are also reachable through the **MCP** server
  (`project_report`, `list_notes`, and the other list/query tools).

If you need any of these programmatically, open an issue — most are additive.

## Notes

- Use cases added from the library are **snapshots**; later edits to the library don't
  change them, and deleting a library entry nulls the `library_id` but keeps the use case.
- List filters: `projects` accepts `status_id`, `customer_id`, `include_archived`;
  lookups and library/library-sets accept `is_active`; the library also accepts
  `category` and `library_set_id`.
