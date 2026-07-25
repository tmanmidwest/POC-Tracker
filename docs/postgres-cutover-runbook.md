# Postgres Cutover — Operator Runbook

Postgres is the application's only database. The AWS deploy scripts
([docs/deploy_to_AWS_fargate/](./deploy_to_AWS_fargate/)) now **provision RDS
automatically**, so a brand-new deployment needs no manual DB steps. This runbook
covers the one case that does: **moving an existing SQLite-on-EFS instance's data
onto Postgres**.

## A. Brand-new deployment (no existing data)

Nothing special — just deploy:

```bash
cd docs/deploy_to_AWS_fargate && ./deploy.sh
```

`deploy.sh` creates a small RDS PostgreSQL instance, stores its connection URL in
Secrets Manager, injects it into the task, and the app **migrates the schema and
seeds itself on first boot**. Log in with the seeded admin and change the password.

Local / non-AWS is the same idea with zero config: `docker compose up` starts
Postgres and points the app at it automatically.

## B. Cutover of an EXISTING instance (preserve current SQLite data)

Your current production runs on SQLite-on-EFS. The data migration is a **one-time,
in-a-maintenance-window** operation — deploying the new code alone does not move
data (and would boot the app against a freshly-seeded empty Postgres).

### B.1 — Grab a consistent copy of the current database

- Put the instance in a maintenance window (stop writes): `./manage.sh stop`, or
  scale the service to 0.
- Copy the live SQLite file off EFS — the `/data/poct.db` file, or `db/poct.db`
  from a recent in-app backup archive. Call it `poct.db` locally.

### B.2 — Deploy the new (Postgres) version

```bash
cd docs/deploy_to_AWS_fargate && ./deploy.sh
```

This provisions RDS and boots the app, which migrates + **seeds a fresh** database.
That seed is about to be replaced by your real data in the next step.

### B.3 — Load your real data into RDS

Get the RDS URL from Secrets Manager (the secret `‹instance›/database-url`), then
run the migration script from a machine that can reach RDS (or briefly make the
instance publicly accessible / use a bastion):

```bash
export POCT_DATABASE_URL="$(aws secretsmanager get-secret-value \
  --secret-id '‹instance›/database-url' --query SecretString --output text)"
export POCT_DATA_DIR=/tmp/poct-migrate

.venv/bin/python -m app.scripts.migrate_sqlite_to_postgres \
    --source /path/to/poct.db \
    --target "$POCT_DATABASE_URL"
```

The script **truncates the freshly-seeded target**, copies every table from the
SQLite file in FK order (preserving ids), resets sequences, and rebuilds the search
index. It refuses to run unless the target is at the same Alembic revision as the
source, and prints a per-table row count — sanity-check it (users, customers,
projects…).

### B.4 — Restart + verify

- Restart the app tasks so they pick up the loaded data (`./update.sh`, or force a
  new deployment).
- Smoke test: log in, open a project, run a search, generate a report/export,
  confirm `GET /health` → `{"database":"ok"}`.

## Files, backups, HA — after cutover

- **DB backups are RDS's job** — automated snapshots + 7-day retention are on;
  restore data via point-in-time recovery. The in-app backup archive now holds
  **uploaded files + keys only**.
- **Uploaded files stay on EFS** (shared across tasks) — no change.
- **HA is on by default**: `deploy.sh` runs `DESIRED_COUNT` app tasks (default 2)
  with zero-downtime rolling deploys. Startup migrations + daily sweeps are
  advisory-locked, so multiple tasks are safe.

## Rollback

If something's wrong after B, the old SQLite file is untouched — redeploy the
previous app version pointed back at SQLite-on-EFS. Keep the `poct.db` copy until
you're confident on Postgres.
