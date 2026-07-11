# External-user invitations

Invite a customer or partner to **view a specific project** (read-only) by email:
its use cases, journal notes, and — once Phase 4 lands — its non-internal-only
tasks. External users are scoped by `ProjectGrant` (see [SCHEMA.md](SCHEMA.md))
and can only see projects shared with them.

This is being built in phases. Each phase is independently shippable.

| Phase | Scope | Status |
|---|---|---|
| 1 | Outbound email (SMTP) admin config + test-send | **Done** |
| 2 | `email` / `company` on users, invite tokens, accept-and-set-password flow | **Done** |
| 3 | Invite from a project's "Shared access" panel; an "External users" box on the Users page | **Done** |
| 4 | External viewers see a project's non-`internal_only` tasks | **Done** |

A follow-on capability, **[Account expiry](#account-expiry-60-day-lifetime)**, gives
external accounts a finite lifetime with auto-deactivation, a pre-expiry SE
warning, and an in-app extend — documented at the end of this file.

---

## Phase 1 — Email (SMTP)

Before anything can be *invited*, the app needs to send mail. Phase 1 adds an
admin-configured outbound SMTP server and nothing else yet consumes it.

### Admin setup — Settings → Email

Admins configure one SMTP server (the settings area is admin-only):

| Field | Notes |
|---|---|
| **SMTP host** | e.g. `smtp.example.com`. Required to enable. |
| **Port** | Defaults to `587`. |
| **Connection security** | `STARTTLS` (usually 587), `SSL/TLS` (usually 465), or `None` (unencrypted — discouraged). |
| **Username / Password** | Optional (some relays accept trusted unauthenticated submission). The password is stored **encrypted at rest** (Fernet, via `secret_box`) and never shown back — leave blank on edit to keep the saved one. |
| **From address / From name** | The envelope/header sender. From address is required to enable. |
| **Enable outbound email** | Master switch. Off = the app sends no mail. Can't be enabled without a host and from address. |

A **Send test email** button (uses the *saved* settings) delivers a fixed
"it works" message so an admin can confirm delivery before relying on it.

### How it's built

- **Model:** `smtp_config` — a single row (fixed `id=1`), mirroring
  `google_tasks_config`. See [app/models/smtp_config.py](../app/models/smtp_config.py).
- **Service:** [app/services/email.py](../app/services/email.py) —
  `get_config` / `set_config` / `is_ready` / `send_email` / `send_test_email`.
  `send_email` builds a `text` + optional `html` message and delivers it over
  `smtplib`, honoring the security mode (plain / STARTTLS / SSL). Failures raise
  `EmailNotConfigured` or `EmailSendError`, which the UI turns into a flash.
- **Secret handling:** the SMTP password uses `encrypt_secret` / `decrypt_secret`
  (Fernet) — recoverable, because it's handed to the server on every send.
- **Audit events:** `email.settings.updated`, and `email.test`
  (`outcome=success|failure`), under the `system` category.

---

## Phase 2 — Invite tokens + accept flow

Adds the user fields and the full invite → accept lifecycle. The **admin entry
points** (a button to actually send an invite) come in Phase 3; Phase 2 delivers
the service, the token/email plumbing, and the public accept page.

### User fields

`app_users` gains two columns (both nullable, backfilled empty on existing rows):

- **email** — unique. For an invited external user this is also their **username /
  login id** (set to the normalized, lowercased email). Existing/internal accounts
  keep `email = NULL`.
- **company** — the external user's organization, shown in the External users list
  (Phase 3).

### The lifecycle

1. **Create** (`invitations.create_invite`) — given an email (+ optional name,
   company, project, inviter): find-or-create the external `AppUser`
   (`is_external`, `is_active`, **no password yet**), grant it the project
   (idempotent `ProjectGrant`), store a **hashed** single-use token, and email a
   link. Refuses to invite an address that belongs to an **internal** account
   (anti-takeover) or one whose email would collide with an existing username.
2. **Deliver** — the email links to `{public_base_url}/invite/{token}`. A public
   base URL is required (`POCT_PUBLIC_BASE_URL`, or passed explicitly); without one,
   creation raises rather than emailing a broken link.
3. **Accept** — the public page (`GET/POST /invite/{token}`, no auth) verifies the
   token (pending + unexpired), takes a new password (min 8, confirmed), sets it,
   activates the account, **logs the user in**, and lands them on their project.
4. **Resend / revoke** (`resend_invite` / `revoke_invite`) — resend rotates to a
   fresh token + expiry; revoke marks the invite so its link stops working.

### Token security

- Tokens are `secrets.token_urlsafe(32)`, stored only as a **SHA-256 hash**
  (`tokens.hash_token`) — the plaintext is emailed once, never persisted (same
  model as API keys).
- **Single-use** (accepting flips status to `accepted`) and **expiring**
  (`INVITE_TTL_DAYS = 7`). Resending issues a new token and invalidates the old one.
- Invited users get **local** accounts only; an invite never links to or elevates
  an existing internal account.

### How it's built

- **Models:** `user_invites` ([app/models/user_invite.py](../app/models/user_invite.py))
  + `email`/`company` on `app_users`. Migration
  [0025](../alembic/versions/0025_add_user_invites.py) (plain `ADD COLUMN`).
- **Service:** [app/services/invitations.py](../app/services/invitations.py).
- **Public routes:** [app/ui/invite_routes.py](../app/ui/invite_routes.py) — mounted
  with no auth dependency so a logged-out invitee can reach it.
- **Audit events:** `invitation.sent`, `invitation.accepted` (category
  `invitation`).

---

## Phase 3 — Invite from the project; manage on the Users page

Wires the Phase-2 service to the UI. Inviting is **project-driven** — there is no
separate "admin invite" surface.

### Invite from a project's "Shared access" panel

The Share panel (`POST /ui/projects/{id}/invite`) gains an **Invite by email**
form (name, company, email). Who can invite mirrors who can already share the
project — an **admin or the project's assigned Sales Engineer** (`can_grant_project`,
self-checked in the route), *not* admins only. Inviting provisions/reuses the
external user, grants them the project, and emails the link in one step. If email
isn't configured, the access is still granted and the admin is told to configure
**Settings → Email** and resend. Grantees who haven't set a password yet show a
**Pending** badge in the panel.

### Manage on the Users page

**Settings → Users** now splits into two boxes: **Internal users** (the existing
table) and a distinct **External users** box showing each external viewer's
company, email, status (**Active / Invited / Expired / Revoked**), the projects
they can view, and last login. Actions:

- **Resend invite** (`POST /ui/settings/admin-users/{id}/resend-invite`) — for
  users who haven't accepted; rotates to a fresh token + expiry and re-emails.
- **Remove** — reuses the existing user-delete route; deleting the external user
  cascades their grants and invites away.

The Users page is admin-only (the whole Settings area is), but *inviting* is not
limited to admins — that happens on the project.

### Audit events

`invitation.sent` (project panel), `invitation.resent` (Users page), plus the
Phase-2 `invitation.accepted`.

---

## Phase 4 — External viewers see project tasks

The project detail page's **Tasks** card now renders for external viewers too
(when the Task Manager module is enabled). They see every task assigned to the
project that isn't marked **Internal only**, regardless of which internal user
owns it — **read-only**.

- **Visibility helper:** `services/tasks.visible_project_tasks(db, project, user)`
  — the task analogue of `visible_project_notes`. Internal standard users see
  their own project tasks; admins see everyone's; external viewers see all
  non-`internal_only` tasks on the project.
- **Read-only UI:** the "+ Add task" button, the inline status dropdown, and the
  Edit link are hidden for external viewers (status shows as a plain badge).
- **Exposure reminder:** internal users see a note on the card — *"Tasks assigned
  to this project are visible to its external viewers unless marked Internal
  only."* The default is visible, so mark sensitive tasks Internal only.

Tasks appear only on the project page, not in the report/PDF/zip (those have no
tasks section for anyone).

With Phase 4, the feature is complete: an invited external viewer can see a shared
project's **use cases, notes, and tasks** — minus anything marked internal-only.

---

## Account expiry (60-day lifetime)

External viewer accounts have a **finite lifetime**. This is separate from the
invite *token's* 7-day expiry (`INVITE_TTL_DAYS`, which only bounds how long the
accept link is valid) — this is the **account** itself expiring after it's in use,
so a customer login left over from a finished POC doesn't stay open forever.

### Lifecycle

1. **Clock starts at acceptance.** When an invitee sets their password, the
   account's `expires_at` is stamped to *acceptance + the configured term* (default
   **60 days**). It's a fixed term — it does not slide with logins.
2. **Warn (7 days before).** A daily sweep emails the Sales Engineer(s) of the
   user's granted project(s) once, ~7 days ahead, so someone can extend it in time.
3. **Expire.** Once past `expires_at`, the sweep sets `is_active = False`. Login is
   already gated on `is_active`, so access stops immediately. Nothing is deleted —
   grants, notes visibility, and history are untouched.
4. **Extend (any time).** An admin or the project's SE picks a new expiry; the
   account is reactivated and the warning is armed again for the new term.

### Configuration — Settings → System

| Setting | Notes |
|---|---|
| **External user lifetime (days)** | The default term applied at acceptance. Ships at **60**; set **0** so external accounts never expire. Env default: `POCT_EXTERNAL_USER_TTL_DAYS`. |

Changing this affects **newly-accepted** users only — it never retroactively
shifts existing accounts' expiry dates (extend those individually instead).

### Who gets warned, and who can extend

- **Warned:** the distinct Sales Engineers (with an email) across every project the
  user can view. If none have an email, it falls back to admins. If SMTP isn't
  configured the email is skipped silently — the in-app **Expires** indicator still
  shows the pending expiry.
- **Can extend:** an **admin**, or the **SE of one of the user's granted projects**
  (same `can_grant_project` rule as inviting). Extend from either the **External
  users** list (Settings → Users) or a project's **Shared access** panel, with a
  picker of **+30 / +60 / +90 days** or a custom date.

### Rollout

Migration [0026](../alembic/versions/0026_add_external_user_expiry.py) backfills
existing **active, accepted** external users to *deploy + 60 days*, so upgrading
never surprise-expires anyone on the first sweep.

### Statuses on the Users page

The External users box distinguishes account state from invite state:

| Badge | Meaning |
|---|---|
| **Active** | Accepted and within its term. |
| **Expired** | Accepted but past `expires_at` (deactivated) — extend to restore. |
| **Invited** | Invite sent, not yet accepted. |
| **Invite expired** | A *pending* invite whose 7-day token lapsed — resend it. |
| **Disabled** | Deactivated for a reason other than expiry. |
| **Revoked** | The invite was revoked. |

### How it's built

- **Model:** `expires_at` + `expiry_warning_sent_at` on `app_users`, and
  `external_user_ttl_days` on `app_config`. Helper props on `AppUser`
  (`is_expired`, `days_until_expiry`, `expires_at_aware`). Migration
  [0026](../alembic/versions/0026_add_external_user_expiry.py).
- **Service:** [app/services/external_expiry.py](../app/services/external_expiry.py)
  — `set_initial_expiry` (called from `invitations.accept_invite`), `expire_due_users`,
  `send_expiry_warnings`, `run_sweep`, `extend_user`, `resolve_extension`.
- **Scheduling:** a once-a-day `asyncio` loop in
  [app/main.py](../app/main.py) `lifespan` (`_external_expiry_loop`), run at startup
  and every 24h — the same pattern as audit-retention pruning. No external cron.
- **Config:** `system_config.current_external_user_ttl_days` /
  `set_external_user_ttl_days` (cached, like audit retention).
- **Routes:** admin extend `POST /ui/settings/admin-users/{id}/extend`; SE extend
  `POST /ui/projects/{project_id}/external/{user_id}/extend`.
- **Audit events** (category `user`): `external_user.expired`,
  `external_user.expiry_warned`, `external_user.extended`.
