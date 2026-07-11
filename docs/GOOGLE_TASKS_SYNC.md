# Google Tasks two-way sync

Per-user, opt-in sync between a user's Questlog tasks and a dedicated
**"POC Tracker"** list in their own Google Tasks account. Built in two increments:

- **Increment 1 — push** (Questlog → Google): task create/update/complete/archive/
  delete is reflected to Google inline on save. See `app/services/google_tasks_sync.py`.
- **Increment 2 — pull + reconcile** (Google → Questlog): changes made in Google
  (edits, completions, new tasks, deletions) flow back into Questlog. This doc.

## Direction & triggers

| Direction | When |
|---|---|
| Push (POC → Google) | Inline on every task save (`sync_after_change`), and for any locally-changed task during a full sync. |
| Pull (Google → POC) | A background poll every 5 min for all connected users, plus a **Sync now** button on the Tasks page. |

Push stays inline so Google is updated immediately; the poll's job is the pull half
plus retrying any push that previously failed (`last_error`).

## Identity & watermark

- Each POC task links to its Google task by `Task.external_id` (+ `external_etag`).
- `Task.last_synced_at` is the per-task watermark: the moment we last reconciled it.
- `UserGoogleCredential.last_sync_at` is the per-user high-water mark passed to Google's
  `tasks.list?updatedMin=…` so each poll only fetches tasks changed since last time.

## Conflict resolution — last-edit-wins

For a task changed on **both** sides since the last sync, the side with the newer
modification time wins the whole record (not a field-level merge — predictable):

- `local_changed`  = `task.updated_at > task.last_synced_at`
- `remote_changed` = `remote.updated > cred.last_sync_at`
- remote-only change → **pull** remote into local.
- local-only change → **push** (handled inline / by the changed-push pass).
- both changed → compare `remote.updated` vs `task.updated_at`; newer wins. Ties → local.

## Field mapping (pull)

| Google | Questlog |
|---|---|
| `title` | `title` |
| `notes` | `details` (plain text; `details_html` is cleared — Google notes carry no HTML) |
| `due` | `due_date` (date part) |
| `status = completed` | move to the default **terminal** status (only if currently open) |
| `status = needsAction` | move to the default **open** status (only if currently terminal) |
| `deleted = true` | **archive** the POC task (soft; recoverable) and unlink `external_id` |

Status is only changed when the done/not-done bit flips, so a richer POC status
("In Progress", "Blocked") is preserved as long as Google agrees on completed-ness.
Default terminal/open statuses are the lowest `sort_order` active status of each kind.

## Google-origin tasks

A task created directly in the user's Google "POC Tracker" list (no matching
`external_id` locally) is **pulled in as a new POC task** owned by that user, with its
status derived from the Google completed bit. Deleted-and-unmatched remotes are ignored.

## Deletions

- **Completed in Google** → POC task moves to Done (terminal), stays visible.
- **Deleted in Google** → POC task is **archived** (never hard-deleted), and its
  `external_id` is cleared so a later restore re-creates a fresh Google task.

## Scheduling

`run_pull_sweep()` runs every `_GOOGLE_SYNC_INTERVAL_SECONDS` (default 300s) from the
app lifespan in `app/main.py`, mirroring the audit-retention and external-expiry loops.
It owns its own session and never raises. Each connected user is synced independently;
one user's failure doesn't stop the others. The **Sync now** button calls the same
per-user path on demand.

## Failure logging

Pull failures record `task.google_pull_failed` (and reuse `task.google_sync_needs_reauth`
when the token is revoked mid-pull) to the activity log — see
`docs`/memory `activity-log-failure-events`.
