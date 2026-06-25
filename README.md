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
python -m app.main                   # serves on http://localhost:8000
```

Migrations run and seed data loads automatically on startup. Open
http://localhost:8000 and sign in with the seeded admin (see
`./data/INITIAL_CREDENTIALS.txt`, default `robbytheadmin` /
`N0nPr0dF0r$@viynt8`). Change the password immediately under **Settings → Users**.

## Quick start (Docker)

```bash
docker compose up --build
# UI on http://localhost:8000 (override with POCT_HOST_PORT)
```

The SQLite database, screenshots, and signing keys persist in the `poct-data` named volume.

## Configuration

All settings are environment variables prefixed `POCT_` (see `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `POCT_DATA_DIR` | `/data` | Directory for the SQLite DB, screenshots, and keys |
| `POCT_BIND_HOST` / `POCT_BIND_PORT` | `0.0.0.0` / `8000` | HTTP bind |
| `POCT_INITIAL_ADMIN_USERNAME` / `POCT_INITIAL_ADMIN_PASSWORD` | `robbytheadmin` / … | Seeded admin |
| `POCT_PUBLIC_BASE_URL` | — | External URL for OIDC redirect URIs behind a proxy |
| `POCT_AUDIT_RETENTION_DAYS` | `30` | Activity-log retention (0 = keep forever) |
| `POCT_HOST_PORT` | `8000` | Host port mapping for docker-compose |

## REST API

Authenticate with an API key (Settings → API Keys) or an OAuth access token:

```bash
curl -H "Authorization: Bearer poct_..." http://localhost:8000/api/v1/projects/
```

Full interactive docs at `/docs`. See [docs/API.md](docs/API.md).

## MCP server

The MCP server exposes read/report tools (list/get projects & customers, the library, and
report generators) over the REST API.

```bash
pip install -e ".[mcp]"
export POCT_MCP_BASE_URL=http://localhost:8000
export POCT_MCP_API_KEY=poct_...      # an API key from the app
poct-mcp                              # stdio transport
```

## Development

```bash
pytest            # run the test suite
ruff check app tests
```

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — requirements and the design decisions behind them
- [docs/SCHEMA.md](docs/SCHEMA.md) — the data model
- [docs/API.md](docs/API.md) — REST API overview
