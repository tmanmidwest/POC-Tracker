# Deploying POC-Tracker to AWS ECS Fargate

These scripts stand up POC-Tracker on **AWS ECS Fargate** — no console clicking, no
hand-written task-definition JSON. You run `./deploy.sh`, answer a couple of
prompts, and ~10 minutes later you have a public URL. Everything lives in your own
AWS account; each teammate can deploy their own independent instance.

The container image is **built from source** and pushed to ECR by the scripts —
there is no public image to pull.

---

## Quick start

```bash
cd docs/deploy_to_AWS_fargate
./setup.sh     # read-only prerequisite + AWS-permission check
./deploy.sh    # builds the image, provisions everything, waits until healthy
```

`deploy.sh` prints the app URL (and the MCP endpoint) when it finishes.

---

## What gets created

| Resource | Purpose |
|---|---|
| ECR repository (`<name>-webapp`) | Stores the image built from this repo |
| ECS cluster + service (`<name>`) | Runs the Fargate task |
| Fargate task (2 containers) | **web app** on `8010` + **MCP server** on `8443` |
| **RDS PostgreSQL** (`<name>-db`) | The application database (small `db.t4g.micro` by default) |
| **Secrets Manager** (`<name>/database-url`) | The DB connection URL, injected into the task |
| EFS filesystem + access point | Persistent `/data` (secrets, keys, uploaded files) |
| Application Load Balancer | Public endpoint(s) — `:80/:443` for the app, `:8443` for MCP |
| Target groups | `<name>-tg` (app) and `<name>-mcp-tg` (MCP) |
| Security groups | `<name>-alb-sg`, `<name>-ecs-sg`, `<name>-db-sg` |
| CloudWatch log group (`/ecs/<name>-webapp`) | Container logs (`ecs/*` = app, `mcp/*` = MCP) |
| IAM `ecsTaskExecutionRole` | Shared, created once if absent (+ per-instance secret-read policy) |

`<name>` is the instance name you choose at deploy time (default `poc-tracker`).

### Architecture

Both containers run in **one Fargate task** and share the same EFS `/data` volume.
Because Fargate's `awsvpc` networking gives containers in a task a shared network
namespace, the MCP server reaches the web app over `http://localhost:8010` — no
service discovery needed. This mirrors the repo's `docker-compose.yml`, where the
MCP server reads its UI-managed gateway token live from the shared volume.

The MCP container is marked **non-essential**: if it crashes, the web app task
keeps running. It also waits for the web app to pass its health check before
starting (`dependsOn: HEALTHY`).

---

## The MCP server

By default both containers deploy. To deploy the **web app only**:

```bash
DEPLOY_MCP=false ./deploy.sh
```

The MCP endpoint is published on the ALB at port **8443** and is **auth-gated** —
it answers `401`/`503` until you generate a gateway token in the app UI
(**Settings → MCP**), so it's safe to expose. Because ALB health-check matchers
only allow codes 200–499 (a `503` can never be "healthy"), the MCP target group
health-checks the **web app's `/health` on port 8010** instead of the MCP port —
i.e. "route MCP traffic to this task whenever the task is up." Clients still
receive the real `503`/`401` from the MCP server. Port **8443** is used (rather
than 8011) because it is one of the HTTPS ports Cloudflare's proxy forwards — so
behind a custom domain the MCP endpoint works through Cloudflare's orange-cloud
proxy just like the app on 443.

Verify it's reachable:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<alb-dns>:8443/
# Any HTTP code (503/401/406) = server is up. Connection refused/hang = not up.
```

Then, in **Settings → MCP**: generate the outbound API token, add an inbound
gateway token, and point your client at `http://<host>:8443/mcp` with
`Authorization: Bearer <gateway-token>` (the streamable-HTTP transport is served
at the `/mcp` path).

---

## HTTPS / custom domain (optional)

By default the app serves plain HTTP on the ALB's generated DNS name. To serve
HTTPS on your own domain, the script provisions a free ACM certificate, adds a
443 listener, redirects HTTP→443, and (when MCP is enabled) adds an HTTPS listener
on 8443 using the **same certificate**.

```bash
ENABLE_HTTPS=true DOMAIN_NAME=poc.trevorcombs.com ./deploy.sh
```

The script prompts you for **exactly two Cloudflare DNS records**, in order:

1. **Certificate validation** (shown mid-run): a one-time CNAME from ACM. Add it
   as **DNS only** (grey cloud). It can stay forever so ACM auto-renews.
2. **Traffic** (shown at the very end): a single CNAME `<domain> → <ALB DNS name>`.
   This one record serves **both** the app (443) and the MCP server (8443) —
   they're the same hostname on the load balancer, just different ports.

Because MCP is on **8443** — a Cloudflare-proxyable HTTPS port — you can set the
traffic record to **Proxied** (orange cloud) and both endpoints work through
Cloudflare. Set **SSL/TLS → encryption mode = Full (strict)**; the ACM cert on the
load balancer keeps that origin hop valid. Prefer to start simple? Set it to **DNS
only** (grey cloud) and both HTTPS URLs still work directly against the ALB.

Resulting endpoints:

| | URL |
|---|---|
| App | `https://<domain>/` |
| MCP | `https://<domain>:8443/mcp` |

---

## Environment variables the deploy sets

The web app container (env prefix `POCT_`):

| Variable | Value | Purpose |
|---|---|---|
| `POCT_DATA_DIR` | `/data` (image default) | Persistent storage (EFS) |
| `POCT_BIND_HOST` / `POCT_BIND_PORT` | `0.0.0.0` / `8010` | Bind address |
| `POCT_LOG_LEVEL` | `INFO` | Log verbosity |
| `POCT_PUBLIC_BASE_URL` | set by `update.sh` (and by `deploy.sh` for HTTPS) | Pins OAuth/redirect base URL |
| `POCT_MCP_PUBLIC_PORT` | `8443` (the MCP port), set when `DEPLOY_MCP=true` | Makes the **Settings → MCP** connection examples show the real reachable URL (`https://<domain>:8443/mcp`) instead of the local `8011` default |

The MCP container additionally gets `POCT_MCP_TRANSPORT=streamable-http`,
`POCT_MCP_HOST=0.0.0.0`, `POCT_MCP_PORT=8443`, and
`POCT_MCP_BASE_URL=http://localhost:8010`. Inbound access (gateway token, allowed
hosts) is managed in the UI and read from the shared volume — no secrets at deploy
time.

To seed a non-default admin password, edit the task definition or set
`POCT_INITIAL_ADMIN_PASSWORD` before first startup (the default seeded login is
`robbytheadmin` / `N0nPr0dF0r$@viynt8` — **change it after first login**).

---

## Day-to-day management

```bash
./manage.sh status    # running state, ALB health, app + MCP URLs
./manage.sh stop      # scale to 0 — Fargate compute charges stop, data kept
./manage.sh start     # resume
./manage.sh restart   # force a new deployment (re-pulls the image)
./manage.sh logs      # stream live CloudWatch logs (Ctrl+C to stop)
./manage.sh url       # print the URLs
```

## Updating to the latest code

After merging to `main` on GitHub:

```bash
./update.sh
```

It clones `main`, rebuilds the image (tagged `latest` **and** the commit SHA),
pushes to ECR, re-registers the task definition (pinning `POCT_PUBLIC_BASE_URL` on
the web app container only), and rolls the ECS service. Since both containers use
the same image, the MCP server updates in the same roll.

> `update.sh` only swaps the image and re-registers the **existing** task
> definition — it does **not** create infrastructure. Use it for routine code
> updates *after* the database is already wired (see below).

## Cutting an existing instance over to Postgres

An instance first deployed on the old SQLite build has no RDS database, no secret,
and no `POCT_DATABASE_URL` in its task. **Run `./deploy.sh` (not `./update.sh`) to
cut it over** — `update.sh` would just redeploy the new image against a database
that isn't there, and the task would fail to start.

```bash
INSTANCE=<name> ./deploy.sh      # idempotent — reuses ECR/ECS/EFS/ALB, ADDS RDS
```

`deploy.sh` provisions the RDS instance, stores its URL in Secrets Manager, wires
it into the task definition's `secrets`, and rolls the service. Notes:

- It **waits ~10 minutes** for the new database to become available on first
  creation — that's expected, not a hang.
- The new database boots **fresh (migrated + seeded)** — the instance's old
  **SQLite data does not carry over**. To preserve it, run the one-time migration
  after the deploy (see [`../postgres-cutover-runbook.md`](../postgres-cutover-runbook.md) §B).
- The service now runs `DESIRED_COUNT` tasks (default **2**, for HA + zero-downtime
  rolling deploys). Set `DESIRED_COUNT=1 ./deploy.sh` to run a single task.

After the cutover, routine updates go back to `./update.sh` — it preserves the
`secrets` wiring across re-registrations.

## Tearing down

```bash
./teardown.sh    # type 'delete' to confirm
```

Deletes the ECS service/cluster, both target groups, the ALB and listeners, the
**RDS database (no final snapshot)** and its secret, EFS (**including your uploaded
files**), security groups, log group, and ECR repository. The shared
`ecsTaskExecutionRole` IAM role is left in place (its per-instance secret policy is removed).

## Recovering state on another machine

The scripts track a deployment via a hidden `.poc-tracker-state*` file. On a new
machine (or if you lose it), rebuild it from live AWS resources:

```bash
./restore-state.sh us-east-1     # pass the region you deployed to
```

---

## Multiple instances

Each instance is a fully isolated stack (own ALB, EFS, containers, URL). Run more
than one in the same account by giving each a distinct name:

```bash
INSTANCE=poc-demo ./deploy.sh
```

State files are namespaced (`.poc-tracker-state`, `.poc-tracker-state.poc-demo`),
and `manage.sh` / `update.sh` / `teardown.sh` let you pick which one to act on.

> Each running instance has its **own load balancer (~$16/month)**. Tear down
> instances you're not using.

---

## Cost (running continuously, `us-east-1`)

| Resource | Approx. monthly |
|---|---|
| Fargate — 2 tasks (0.5 vCPU / 1 GB each) | ~$36 |
| Application Load Balancer | ~$16 |
| RDS `db.t4g.micro` (single-AZ, 20 GB gp3) | ~$13–15 |
| EFS + CloudWatch + Secrets Manager | ~$2–3 |
| **Total** | **~$67/month** |

Cost levers: set `DESIRED_COUNT=1` for a single app task (~$18 less, but no
zero-downtime deploys / HA); `DEPLOY_MCP=false` with `CPU=256 / MEMORY=512` cuts
Fargate further. `DB_MULTI_AZ=true` roughly doubles the RDS line for a standby.
`./teardown.sh` stops all charges (**including deleting the database**).

---

## Notes & caveats

- **Postgres is the database (RDS).** The app connects via `POCT_DATABASE_URL`,
  injected from Secrets Manager — never a plaintext env var. Because Postgres
  handles concurrent writers, the service runs **`DESIRED_COUNT` tasks (default 2)**
  with a normal **zero-downtime rolling deploy** (`max 200% / min 100%`); startup
  migrations and the daily sweeps are advisory-locked so only one task runs them.
- **DB backups are RDS's job.** Automated snapshots + 7-day retention are on by
  default (tune `--backup-retention-period`); use point-in-time recovery to
  restore data. The in-app backup archive holds **uploaded files + keys only**.
- **`/data` (EFS) holds files, session secret, and MCP token** — no longer the
  database. Teardown deletes the RDS instance (no final snapshot), its secret,
  and the EFS filesystem.
- **Toggling MCP on an existing deployment:** re-running `deploy.sh` with a
  different `DEPLOY_MCP` reconciles the security-group rules, target group,
  listener, and service load-balancer wiring, then rolls a new task definition.
- **First deploy takes ~10 min** (image build + EFS mount targets + ALB health
  checks). Subsequent `update.sh` runs are faster.
