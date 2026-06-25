# POC Tracker

A lightweight web app for sales/solutions engineers to track proof-of-concept (POC)
engagements: customers and contacts, projects, use cases (from a reusable library or
ad-hoc), screenshots, a customizable dashboard, and reporting. It ships a REST API and
an MCP server so other tools and AI assistants can read and report on the data.

> Non-production demo software. Change the default admin password before any real use.

## Features

- **Local authentication** out of the box, plus optional **OIDC single sign-on** (add any
  OAuth/OIDC identity provider in the UI).
- **Two groups:** **Admins** can do anything; **standard users** can add/edit POC
  projects and their use cases.
- **API keys** and **OAuth client-credentials** for machine-to-machine access to the REST API.
- **Customers & contacts** — contacts carry a role picked from a master list (Champion,
  Technical Stakeholder, …).
- **Projects** — status (from a global list), start/end dates, an assigned Sales Engineer
  (an app user), and an Account Executive (tracked by reference; AEs don't log in).
- **Use cases** — pull from a master **library** when building a project, add the customer's
  own **ad-hoc** ones, per-project reference numbers, status, comments, and **screenshot uploads**.
  Library entries are copied in as **snapshots**, so editing the library never changes a POC
  that's already in flight.
- **Dashboard** — projects grouped by status, with per-user preferences (columns, which
  statuses to show, sort).
- **Reporting** — an all-POCs overview and a print-friendly single-POC report.
- **Activity log** — persisted audit events with a viewer and JSON/CSV export.
- **REST API** (`/api/v1`, OpenAPI at `/docs`) and an **MCP server** for AI-driven queries/reports.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export POCT_DATA_DIR=./data          # where the SQLite DB + keys live
python -m app.main                   # serves on http://localhost:8010
```

Migrations run and seed data loads automatically on startup. Open
http://localhost:8010 and sign in with the seeded admin (see
`./data/INITIAL_CREDENTIALS.txt`, default `robbytheadmin` /
`N0nPr0dF0r$@viynt8`). Change the password immediately under **Settings → Users**.

## Quick start (Docker)

```bash
docker compose up --build
# UI on http://localhost:8010 (override with POCT_HOST_PORT)
```

The SQLite database, screenshots, and signing keys persist in the `poct-data` named volume.

### The two containers

The compose stack defines two services that **both start by default** — the **app** (`8010`)
and the **MCP server** (`8011`):

```bash
docker compose up -d --build
```

You should see **two** containers: `poc-tracker` (app, 8010) and `poc-tracker-mcp` (MCP, 8011).
The MCP server needs **no secrets to start** — it shares the data volume, so you do all the
setup in the UI under **Settings → MCP**: generate the **gateway token** (what your gateway
presents) and the **API token** (what the MCP server uses to call the app). Until the gateway
token exists, the MCP endpoint returns `503`. Rotating either one needs no restart.

Don't need the MCP server? Comment out the `mcp` service in `docker-compose.yml`.

## Configuration

All settings are environment variables prefixed `POCT_` (see `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `POCT_DATA_DIR` | `/data` | Directory for the SQLite DB, screenshots, and keys |
| `POCT_BIND_HOST` / `POCT_BIND_PORT` | `0.0.0.0` / `8010` | HTTP bind |
| `POCT_INITIAL_ADMIN_USERNAME` / `POCT_INITIAL_ADMIN_PASSWORD` | `robbytheadmin` / … | Seeded admin |
| `POCT_PUBLIC_BASE_URL` | — | External URL for OIDC redirect URIs behind a proxy |
| `POCT_AUDIT_RETENTION_DAYS` | `30` | Activity-log retention (0 = keep forever) |
| `POCT_HOST_PORT` | `8010` | Host port mapping for docker-compose |

## REST API

Authenticate with an API key (Settings → API Keys) or an OAuth access token:

```bash
curl -H "Authorization: Bearer poct_..." http://localhost:8010/api/v1/projects/
```

Full interactive docs at `/docs`. See [docs/API.md](docs/API.md).

## MCP server

The MCP server lets an AI assistant both **read** and **write** POC data over the REST API:

- **Query/report** — `list_projects`, `find_projects`, `get_project`, `list_customers`,
  `list_use_case_library`, `list_lookups`, `all_pocs_summary`, `project_report`.
- **Write** — `add_custom_use_cases` (bulk-add a list of use cases to a project — the main
  one for "here's a list, add them"), `add_custom_use_case`, `add_use_cases_from_library`,
  `update_use_case`, `set_use_case_status`, `delete_use_case`, `create_project`,
  `create_customer`.

Status and feature-type arguments accept a **name or id** (resolved case-insensitively), so
an agent can say `status="Completed"` or `feature_type="JML"` without looking up ids.

```bash
pip install -e ".[mcp]"
export POCT_MCP_BASE_URL=http://localhost:8010
poct-mcp                              # stdio transport
```

### Credentials — all managed from the UI (Settings → MCP)

There are **two** separate credentials, and both are generated/rotated in the app UI and read
**live** from the data volume — so the MCP server (and its container) needs **no secrets at
deploy time**, and rotating takes effect on the next call with no restart:

- **Outbound — MCP server → app.** The API token the MCP server uses to call the REST API.
  Generate it under **Settings → MCP**. (Resolution: `POCT_MCP_API_KEY` env override →
  `POCT_MCP_API_KEY_FILE` → the UI-managed file `<POCT_DATA_DIR>/mcp_api_key`.)
- **Inbound — gateway → MCP server** (only for the HTTP transports below). The bearer token a
  gateway must present, plus an optional Host allow-list. Generate them under **Settings →
  MCP** too. Until a gateway token exists, the HTTP endpoint **rejects every call with `503`**,
  so it's safe to start the MCP server before configuring it. (Resolution: `POCT_MCP_AUTH_TOKEN`
  / `POCT_MCP_ALLOWED_HOSTS` env overrides → the UI-managed files.)

Both UI-managed paths require the MCP server to **share the app's data volume**. A *remote*
MCP host that can't see the volume uses the env overrides instead.

For auto-rotation to work, the MCP server must share the app's `POCT_DATA_DIR` (same host, or
the same Docker volume). A **remote** MCP host can't read that file — set `POCT_MCP_API_KEY`
there instead.

### Use it from Claude Desktop

The app must be running (so the MCP server can reach its REST API) and you need an API key
from **Settings → API Keys**. Add the server to your `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "poc-tracker": {
      "command": "/absolute/path/to/POC-Tracker/.venv/bin/poct-mcp",
      "env": {
        "POCT_MCP_BASE_URL": "http://localhost:8010",
        "POCT_MCP_API_KEY": "poct_your_key_here"
      }
    }
  }
}
```

Claude Desktop launches the command directly (no shell), so use the **absolute path** to the
`poct-mcp` entry point in your virtualenv. If you didn't install the package, you can run the
module instead:

```json
{
  "mcpServers": {
    "poc-tracker": {
      "command": "/absolute/path/to/POC-Tracker/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/POC-Tracker",
        "POCT_MCP_BASE_URL": "http://localhost:8010",
        "POCT_MCP_API_KEY": "poct_your_key_here"
      }
    }
  }
}
```

Restart Claude Desktop, then ask it to e.g. *"add these use cases to the Acme POC"* with a
list — it will call `find_projects` then `add_custom_use_cases`. The same config shape works
for any stdio MCP client (Cursor, custom Agent SDK clients, etc.).

### Remote gateways (Saviynt, etc.) — HTTP transport

Stdio has no URL. Gateways that ask for a **Base URL** and an **MCP Endpoint** speak MCP over
HTTP, so run the server with the **streamable-http** transport. **No secrets are needed at
startup** — the endpoint stays locked (returns `503`) until you generate the gateway token in
the UI, then reads it live:

```bash
export POCT_MCP_TRANSPORT=streamable-http
export POCT_MCP_HOST=0.0.0.0          # 0.0.0.0 so a remote gateway can reach it
export POCT_MCP_PORT=8011
export POCT_MCP_BASE_URL=http://localhost:8010        # where the POC Tracker app runs
export POCT_DATA_DIR=/path/to/shared/data             # same data dir as the app (for both tokens)
poct-mcp
```

Then, in the app UI under **Settings → MCP**, generate the **gateway token** (and, if needed,
set **allowed hosts**) and the **API token**. The MCP server picks both up live from the shared
volume — no restart.

| Credential | Direction | Where it's managed |
|---|---|---|
| Gateway token | gateway → MCP server | **Settings → MCP** (the bearer the gateway presents) |
| API token | MCP server → app | **Settings → MCP** (lets the MCP server call the REST API) |

**Three addresses in play:**

| Layer | Address | Notes |
|---|---|---|
| POC Tracker **app** (REST API) | `http://<host>:8010` | `POCT_MCP_BASE_URL` points the MCP server here |
| POC Tracker **MCP server** | `http://<host>:8011/mcp` | what the Saviynt gateway connects to |

In the Saviynt gateway, enter:

- **Base URL:** `http://<mcp-server-host>:8011` (the host/IP where `poct-mcp` runs — use a
  routable address, not `localhost`, if Saviynt is on another machine)
- **MCP Endpoint:** `/mcp`
- **Authorization / bearer token:** the **gateway token** from Settings → MCP

If the gateway only supports the older **SSE** transport, set
`POCT_MCP_TRANSPORT=sse` and use **MCP Endpoint** `/sse` instead.

> **Host allow-list:** by default any Host header is accepted (bearer auth is the gate). To
> harden against DNS-rebinding, set **allowed hosts** under Settings → MCP (comma-separated,
> `:*` wildcards allowed, e.g. `mcp.example.com:8011,10.0.0.5:*`); a non-matching Host then
> gets `403`. Keep port 8011 on a trusted network or behind the gateway.

## Deploying behind a Cloudflare Tunnel

The app and the MCP server are two services on two ports, so they get **two public
hostnames** on the same tunnel — add a second ingress rule / public hostname exactly like the
first:

| Public hostname | Tunnel origin (internal) | Service |
|---|---|---|
| `poctracker.example.com` | `http://docker-host:8010` | the app (UI + REST API) |
| `poctracker-mcp.example.com` | `http://docker-host:8011` | the MCP server |

Cloudflare terminates TLS at the edge, so clients use `https://…` even though the tunnel
origin is plain `http`. In the gateway, the MCP **Base URL** is the `https://` public hostname.

**Two things that will trip you up:**

1. **If you set an allow-list, include the public Host header.** Cloudflare forwards the
   *public* hostname as the `Host` header. By default any host is accepted (bearer auth is the
   gate), so this only matters if you turn on the allow-list under **Settings → MCP** — then add
   `poctracker-mcp.example.com` (and `poctracker-mcp.example.com:*`) to it, or a matching Host
   gets `403`.

2. **Keep `POCT_MCP_BASE_URL` internal.** That's the MCP-server → app hop and never leaves
   your network — point it at the internal `http://docker-host:8010`, not the public URL, so it
   doesn't round-trip out through Cloudflare and back.

**Security:** a tunnel makes the MCP endpoint internet-reachable, gated only by the bearer
token. Since the write tools mutate data, put **Cloudflare Access (Zero Trust)** in front of
`poctracker-mcp.example.com` as well — a **service token** is a clean fit when a single gateway
(e.g. Saviynt) is the only caller — so the bearer secret isn't the sole line of defense.

## Development

```bash
pytest            # run the test suite
ruff check app tests
```

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — requirements and the design decisions behind them
- [docs/SCHEMA.md](docs/SCHEMA.md) — the data model
- [docs/API.md](docs/API.md) — REST API overview
