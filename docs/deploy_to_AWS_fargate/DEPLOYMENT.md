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
| EFS filesystem + access point | Persistent `/data` (SQLite DB, secrets, keys) |
| Application Load Balancer | Public endpoint(s) — `:80/:443` for the app, `:8443` for MCP |
| Target groups | `<name>-tg` (app) and `<name>-mcp-tg` (MCP) |
| Security groups | `<name>-alb-sg`, `<name>-ecs-sg` |
| CloudWatch log group (`/ecs/<name>-webapp`) | Container logs (`ecs/*` = app, `mcp/*` = MCP) |
| IAM `ecsTaskExecutionRole` | Shared, created once if absent |

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
(**Settings → MCP**), so it's safe to expose. Its ALB target-group health check
accepts any `200-499` response as healthy for exactly this reason. Port **8443**
is used (rather than 8011) because it is one of the HTTPS ports Cloudflare's proxy
forwards — so behind a custom domain the MCP endpoint works through Cloudflare's
orange-cloud proxy just like the app on 443.

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

## Tearing down

```bash
./teardown.sh    # type 'delete' to confirm
```

Deletes the ECS service/cluster, both target groups, the ALB and listeners, EFS
(**including your SQLite data**), security groups, log group, and ECR repository.
The shared `ecsTaskExecutionRole` IAM role is left in place.

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
| Fargate (0.5 vCPU / 1 GB, both containers) | ~$18 |
| Application Load Balancer | ~$16 |
| EFS + CloudWatch | ~$1–2 |
| **Total** | **~$35/month** |

Web-app-only (`DEPLOY_MCP=false`) can drop to `CPU=256 / MEMORY=512` (edit the top
of `deploy.sh`), cutting Fargate to ~$9. Run `./manage.sh stop` when idle to
eliminate compute charges; `./teardown.sh` to stop all charges.

---

## Notes & caveats

- **Single replica only.** SQLite can't handle concurrent writers — desired count
  stays at 1. Don't scale the service.
- **`/data` is non-negotiable.** It holds the DB, session secret, and MCP token
  files. Teardown deletes the EFS filesystem and everything on it.
- **Toggling MCP on an existing deployment:** re-running `deploy.sh` with a
  different `DEPLOY_MCP` reconciles the security-group rules, target group,
  listener, and service load-balancer wiring, then rolls a new task definition.
- **First deploy takes ~10 min** (image build + EFS mount targets + ALB health
  checks). Subsequent `update.sh` runs are faster.
