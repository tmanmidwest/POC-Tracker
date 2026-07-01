# Task Manager & Google Tasks sync

A per-user task manager that lives alongside POC projects, plus an optional,
per-user **two-way** sync to each user's own Google Tasks account.

- [Overview](#overview)
- [Statuses & priorities (admin-managed)](#statuses--priorities-admin-managed)
- [Task fields](#task-fields)
- [The tasks dashboard](#the-tasks-dashboard)
- [Tasks on a project](#tasks-on-a-project)
- [Ownership & visibility](#ownership--visibility)
- [REST API & MCP](#rest-api--mcp)
- [Google Tasks sync](#google-tasks-sync)
  - [How it works](#how-it-works)
  - [Field mapping](#field-mapping)
  - [Sync behavior & semantics](#sync-behavior--semantics)
  - [Admin setup — Google Cloud](#admin-setup--google-cloud)
  - [Connecting (per user)](#connecting-per-user)
  - [Security](#security)
  - [Troubleshooting](#troubleshooting)
- [Build status](#build-status)

---

## Overview

Tasks are **owned by the user who creates them** — unlike projects (shared team
data), each user manages their own list. A task has a title, a status, an
optional priority, optional start/due dates, rich-text details, and can be
**assigned to a POC project** (optional).

The whole module is toggleable by an admin under **Settings → System → Task
Manager**. When off, the Tasks nav item and pages disappear (existing tasks are
kept). Enable it and internal users get a **Tasks** item in the sidebar.

## Statuses & priorities (admin-managed)

Both are global lookups, managed under **Settings → Lookups**, and apply to
every user's tasks (they are not per-user):

- **Task Statuses** — `name`, `sort_order` (drives the dashboard grouping/order),
  `is_terminal` (a "done" status). Seeded: **To Do, In Progress, Blocked, Done**.
- **Task Priorities** — `name`, `sort_order`, `color` (hex, drives the badge).
  Seeded: **Low, Medium, High, Urgent**.

Seed (`is_system`) rows can't be deleted, and a status/priority still used by a
task can't be deleted (deactivate it instead).

## Task fields

| Field | Notes |
|---|---|
| **Title** | Required. |
| **Status** | Required; from Task Statuses. |
| **Priority** | Optional; from Task Priorities. |
| **Project** | Optional single project (or none — a standalone personal to-do). |
| **Start date / Due date** | Both optional. |
| **Details** | Rich text (the same Quill editor + sanitizer as project notes). Stored as sanitized HTML plus a plain-text rendering for search/export. |
| **Archived** | Tasks archive (reversible) rather than being deleted, mirroring projects. |

## The tasks dashboard

`/ui/tasks` groups tasks by status, mirroring the project dashboard. Each user
customizes their own view under **Tasks → Customize**: which columns (project,
priority, start, due, owner), which statuses/priorities to show, the sort
(recently updated / due date / priority / title), and whether to include
archived tasks. Preferences are stored per user (`task_dashboard_prefs`).

## Tasks on a project

A project's detail page has a **Tasks** card listing the tasks assigned to that
project (the viewer's own; admins see everyone's), with inline status changes and
an **+ Add task** button that pre-fills the project. Deleting a project doesn't
delete its tasks — their `project_id` is set null, so they survive unassigned.

## Ownership & visibility

- A user sees and manages **their own** tasks.
- **Admins can view all** users' tasks (a "Show tasks for: My / All users"
  toggle appears for admins in the dashboard preferences).
- **External viewers** have no task access at all (the routes are internal-only).

## REST API & MCP

Because the REST API and MCP authenticate as a **machine identity** (an API key
or OAuth client, not a logged-in user), the task endpoints operate **admin-wide**
and take an explicit **`owner`** (username or user id) on create/update — the
caller says whose task it is.

**REST** (`/api/v1`, bearer auth):

- `GET /tasks/` — list across users; filter by `owner`, `status_id`,
  `priority_id`, `project_id`, `include_archived`.
- `GET /tasks/{id}`, `POST /tasks/`, `PATCH /tasks/{id}`, `DELETE /tasks/{id}`.
- `status` / `priority` accept a **name or id**; `details` is sanitized.
- Task lookups: `GET|POST /task-statuses/`, `GET|POST /task-priorities/`,
  and `…/{id}` (PATCH/DELETE).
- All task endpoints return `404` when the module is disabled.

**MCP tools:** `list_tasks`, `get_task`, `create_task`, `update_task`,
`set_task_status`, `delete_task`; `list_lookups` also returns `task_statuses`
and `task_priorities`. Example: `create_task(owner="robby", title="Prep demo",
status="To Do", priority="High", project_id=1)`.

> A later phase can bind an API key to a specific user so `owner` defaults from
> the key (see [REQUIREMENTS.md](REQUIREMENTS.md)); today it's explicit.

---

## Google Tasks sync

Optional, **per-user**, **two-way** sync between a user's POC Tracker tasks and
**their own** Google Tasks account.

### How it works

- **The admin registers one Google OAuth client** (a single app identity — see
  [setup](#admin-setup--google-cloud)). This is *not* a shared Google account.
- **Each user connects their own Google account** from the Tasks page. We store
  that user's own encrypted refresh token (`user_google_credentials`, one row per
  user). User A's tasks sync to A's account, B's to B's — nobody shares anything.
- On connect, the app creates (or reuses) a dedicated **"POC Tracker"** list in
  the user's Google Tasks and syncs all their existing active tasks into it.
- **Connected means all your tasks sync** — there's no per-task opt-in.

### Field mapping

Google Tasks is intentionally minimal, so some fields don't round-trip:

| POC Tracker | Google Tasks | Notes |
|---|---|---|
| Title | `title` | |
| Details | `notes` | **Plain text only** — Google notes carry no HTML. |
| Due date | `due` | Date at midnight UTC. |
| Status | `needsAction` / `completed` | A **terminal** status (e.g. "Done") → `completed`. |
| Start date | — | No Google Tasks equivalent. |
| Priority | — | No Google Tasks equivalent. |
| Project | — | No Google Tasks equivalent. |

### Sync behavior & semantics

These are the decisions the two-way design settled on:

| Situation | Behavior |
|---|---|
| Task changed on **both** sides since last sync | **Last edit wins** (by timestamp). |
| Task **deleted in Google** | The POC task is **archived** (reversible), not deleted. |
| Task **created directly in the Google "POC Tracker" list** | **Imported** as a new POC task for that user (default status, no project). |
| Completion | Any terminal POC status → `completed`; coming back, `completed` maps to your lowest-sorted terminal status ("Done"). |
| Google-side **note edit** | Flattens the POC task's `details` to plain text (HTML formatting only survives for edits made in POC Tracker). |
| POC task **archived** | Removed from the Google list (its link is cleared, so restoring re-creates it). |

**Timing.** Pushes (POC → Google) happen **inline** on save, but *best-effort* —
a Google outage never blocks saving a task; the failure is recorded and retried.
Pulls (Google → POC) run on a periodic reconcile pass plus on opening the Tasks
page, so Google-side changes appear with a short delay (polling; Google Tasks has
no push webhooks).

### Admin setup — Google Cloud

You register **one** OAuth client for the whole app. Users then connect their own
accounts through it. All of this is a one-time operator task in the
[Google Cloud console](https://console.cloud.google.com/).

1. **Create/select a project** in the Google Cloud console.
2. **Enable the Google Tasks API** — *APIs & Services → Library →* search
   "Google Tasks API" → **Enable**.
3. **Configure the OAuth consent screen** (*APIs & Services → OAuth consent
   screen*):
   - **User type = External** — required when users have personal / non-org
     Google accounts. (Use **Internal** only if every user is in a single Google
     Workspace organization; then no verification is needed.)
   - Add the scope **`https://www.googleapis.com/auth/tasks`** (plus `openid` and
     `email`, which the app uses to show which account is connected).
   - **Publishing status** matters:
     - **Testing** (default): only Google accounts you add under **Test users**
       can connect (max 100), and their granted access **expires ~every 7 days**,
       so those users are re-prompted to reconnect (POC Tracker shows a
       **Reconnect** button — this is expected, not a bug).
     - **In production** (published): removes those limits. Because
       `.../auth/tasks` is a **sensitive** scope, Google may require **app
       verification** before you can publish. Once verified, connections are
       long-lived.
4. **Create the OAuth client** (*APIs & Services → Credentials → Create
   credentials → OAuth client ID*):
   - **Application type = Web application**.
   - **Authorized redirect URI** = the value shown on **Settings → Google Tasks**
     in POC Tracker (it's `<your app base URL>/ui/tasks/google/callback`). Copy it
     exactly — a mismatch causes a redirect error at consent time.
   - Copy the generated **Client ID** and **Client secret**.
5. **In POC Tracker, Settings → Google Tasks:** paste the Client ID and secret,
   tick **Enable Google Tasks sync**, and save. The secret is stored **encrypted
   at rest** (Fernet) and never shown back.

> The app must be reachable at a stable **HTTPS** URL that matches the registered
> redirect URI for real users (behind a proxy/tunnel, ensure forwarded-proto is
> honored — the app already trusts `X-Forwarded-Proto`). For local development
> you can register an `http://localhost:8010/...` redirect URI.

### Connecting (per user)

Once an admin has enabled the integration, each user opens **Tasks** and clicks
**Connect Google Tasks**, consents at Google, and is returned to POC Tracker.
The dashboard banner then shows **Connected** (with the account email) and a
**Disconnect** button. Disconnecting revokes the token at Google and drops the
stored credential; existing Google tasks are left as-is.

### Security

- Refresh tokens are **encrypted at rest** with Fernet
  (`app.services.secret_box`), the same mechanism as AI-provider keys and OIDC
  client secrets. Access tokens are short-lived and minted on demand — never
  stored.
- The OAuth flow uses **PKCE (S256)** and a **`state`** anti-CSRF token.
- Scopes are minimal: identity (`openid email`) plus `auth/tasks`.
- Disconnect (and best-effort on errors) **revokes** the grant at Google.
- Task content is the user's own data pushed to the user's own Google account,
  per explicit opt-in.

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Banner says **"needs reconnecting"** | The refresh token was rejected — usually the weekly expiry while the OAuth app is in **Testing**, or the user revoked access. Click **Reconnect**. Publish/verify the app to remove the weekly expiry. |
| **redirect_uri_mismatch** at Google | The redirect URI registered on the OAuth client doesn't exactly match the one on **Settings → Google Tasks**. Copy it verbatim (scheme, host, port, path). |
| **"Google did not return a refresh token"** | Happens if a prior grant exists without offline access. The app requests `access_type=offline` + `prompt=consent`, so reconnecting resolves it; otherwise remove POC Tracker from the account's [third-party access](https://myaccount.google.com/permissions) and reconnect. |
| A user can't connect at all (Testing mode) | Add their Google address under **Test users** on the consent screen, or publish the app. |
| "Connect Google Tasks" doesn't appear | The admin hasn't **enabled** the integration (or filled in client id/secret) under **Settings → Google Tasks**, or the Task Manager module is disabled. |
| Sync silently stops | Check the credential's **last error** (shown as a small "last sync had an issue" badge on the dashboard) and the server logs (`google_push_failed`). |

---

## Build status

- **Task Manager** (UI + REST API + MCP) — **shipped**.
- **Google Tasks — push (POC → Google)** — **shipped** (increment 1): connect,
  create/update/complete/archive/delete push, disconnect, best-effort with retry.
- **Google Tasks — pull (Google → POC)** — **planned** (increment 2): the
  reconcile poll that applies Google-side changes, conflict resolution,
  archive-on-Google-delete, and import of Google-created tasks. The credential's
  `last_sync_at` is the high-water mark it will use.

Everything Google-related is built and tested against a mocked Google backend; no
live Google project is needed to develop, only to actually sync.
