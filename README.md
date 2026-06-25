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
export POCT_MCP_API_KEY=poct_...      # an API key from the app
poct-mcp                              # stdio transport
```

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

## Development

```bash
pytest            # run the test suite
ruff check app tests
```

## Documentation

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) — requirements and the design decisions behind them
- [docs/SCHEMA.md](docs/SCHEMA.md) — the data model
- [docs/API.md](docs/API.md) — REST API overview
