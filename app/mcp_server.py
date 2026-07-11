"""MCP server for Questlog.

Exposes tools so an AI assistant can query POC data, generate reports, and make
changes — in particular, take a list of use cases from a conversation and push
them into a project (requirement: "MCP Server to allow for AI to contact the
platform to read from and then generate reports or query data").

It talks to the running app's REST API using an API key, so it inherits the
app's auth and stays decoupled from the database. Configure with:

    POCT_MCP_BASE_URL    base URL of the running app (default http://localhost:8010)
    POCT_MCP_API_KEY     fixed API key override (optional). Leave UNSET to use the
                         UI-managed, rotatable token (Settings → MCP), which is read
                         live from the data volume so rotations need no restart.
    POCT_MCP_API_KEY_FILE  explicit path to a token file (optional)

Transport (stdio is local-only; the HTTP transports are network-facing and
require a bearer token — the server refuses to start an open endpoint):

    POCT_MCP_TRANSPORT     stdio (default) | streamable-http (/mcp) | sse (/sse)
    POCT_MCP_HOST          bind host for HTTP transports (default 127.0.0.1)
    POCT_MCP_PORT          bind port for HTTP transports (default 8011)

The HTTP transports are gated by a gateway bearer token + optional Host allow-list
that are managed in the app UI (Settings → MCP) and read live from the data volume,
so the server needs no secrets at deploy time. These env vars OVERRIDE the
UI-managed files (useful for a remote MCP host that can't see the volume):

    POCT_MCP_AUTH_TOKEN    gateway bearer token override
    POCT_MCP_ALLOWED_HOSTS comma-separated Host allow-list override

Run it:

    poct-mcp
    # or
    python -m app.mcp_server

The write tools mutate data using the API key's permissions. Lookups (status,
feature type) may be passed by name or id — names are resolved case-insensitively.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("POCT_MCP_BASE_URL", "http://localhost:8010").rstrip("/")
API_KEY = os.environ.get("POCT_MCP_API_KEY", "")

# Transport for `poct-mcp`:
#   stdio           — default; for local clients (Claude Desktop, Cursor)
#   streamable-http — HTTP at <host>:<port>/mcp (for gateways like Saviynt)
#   sse             — HTTP at <host>:<port>/sse (older HTTP transport)
MCP_TRANSPORT = os.environ.get("POCT_MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("POCT_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("POCT_MCP_PORT", "8011"))
# Inbound access control for the HTTP transports (the bearer tokens a gateway must
# present, and the optional Host allow-list) is managed in the app UI and read
# live from the data volume — see app.services.mcp_gateway_tokens (tokens),
# app.services.mcp_gateway (allowed hosts), and GatewayAuthMiddleware below.
# POCT_MCP_AUTH_TOKEN / POCT_MCP_ALLOWED_HOSTS still override the UI-managed files
# for remote MCP hosts that can't see the volume.

mcp = FastMCP("poc-tracker", host=MCP_HOST, port=MCP_PORT)

# Lazily-created HTTP session. Tests inject a TestClient here.
_session: httpx.Client | None = None


def _resolve_token() -> str | None:
    """Resolve the API token to authenticate to the app, freshly each call.

    Order: POCT_MCP_API_KEY (fixed override) → POCT_MCP_API_KEY_FILE → the
    UI-managed token file on the app's data volume. Reading live means rotating
    the token in the UI takes effect on the very next call, no restart.
    """
    if API_KEY:
        return API_KEY
    file_env = os.environ.get("POCT_MCP_API_KEY_FILE")
    if file_env:
        path = Path(file_env)
        return path.read_text().strip() or None if path.exists() else None
    try:
        from app.services.mcp_token import read_token

        return read_token()
    except Exception:  # app package/config not importable in this context
        return None


def _http() -> httpx.Client:
    global _session
    if _session is None:
        _session = httpx.Client(base_url=f"{BASE_URL}/api/v1", timeout=30.0)
    return _session


def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call the REST API and return parsed JSON, raising a clean error on failure."""
    client = _http()
    token = _resolve_token()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    if headers is None and not any(k.lower() == "authorization" for k in client.headers):
        raise RuntimeError(
            "No MCP API token found. Generate one in the app (Settings → MCP), "
            "or set POCT_MCP_API_KEY / POCT_MCP_API_KEY_FILE."
        )
    resp = client.request(method, path, json=json, params=params, headers=headers)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {detail}")
    if resp.status_code == 204:
        return None
    return resp.json()


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, body: dict[str, Any]) -> Any:
    return _request("POST", path, json=body)


def _patch(path: str, body: dict[str, Any]) -> Any:
    return _request("PATCH", path, json=body)


def _delete(path: str) -> Any:
    return _request("DELETE", path)


# ---------------------------------------------------------------------------
# Lookup name resolution
# ---------------------------------------------------------------------------


def _name_map(endpoint: str) -> dict[str, int]:
    """Map lower-cased lookup names to ids for one lookup endpoint."""
    return {row["name"].strip().lower(): row["id"] for row in _get(endpoint)}


def _resolve(value: Any, mapping: dict[str, int], kind: str) -> int | None:
    """Resolve a lookup value (id, numeric string, or name) to an id."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    key = str(value).strip().lower()
    if key in mapping:
        return mapping[key]
    raise ValueError(
        f"Unknown {kind}: {value!r}. Choices: {', '.join(sorted(mapping)) or '(none)'}"
    )


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_projects(
    status_id: int | None = None,
    customer_id: int | None = None,
    include_archived: bool = False,
) -> list[dict]:
    """List POC projects, optionally filtered by status or customer.

    Returns each project with its customer, status, sales engineer, account
    executive, and dates.
    """
    params: dict[str, Any] = {"include_archived": include_archived}
    if status_id is not None:
        params["status_id"] = status_id
    if customer_id is not None:
        params["customer_id"] = customer_id
    return _get("/projects/", params)


@mcp.tool()
def find_projects(query: str) -> list[dict]:
    """Find projects whose customer name or project name contains `query`
    (case-insensitive). Useful for turning a customer name into a project id."""
    q = query.strip().lower()
    out = []
    for p in _get("/projects/", {"include_archived": True}):
        name = (p.get("name") or "").lower()
        customer = (p.get("customer") or {}).get("name", "").lower()
        if q in name or q in customer:
            out.append(p)
    return out


@mcp.tool()
def get_project(project_id: int) -> dict:
    """Get one project in full, including all of its use cases and their status."""
    return _get(f"/projects/{project_id}")


@mcp.tool()
def list_customers() -> list[dict]:
    """List all customers (prospects)."""
    return _get("/customers/")


@mcp.tool()
def get_customer(customer_id: int) -> dict:
    """Get one customer with its contacts."""
    return _get(f"/customers/{customer_id}")


@mcp.tool()
def list_library_sets() -> list[dict]:
    """List the named use-case libraries (e.g. 'Standard', or per-product
    libraries). Use a library's id to scope list_use_case_library."""
    return _get("/library-sets/")


@mcp.tool()
def list_use_case_library(
    category: str | None = None, library_set_id: int | None = None
) -> list[dict]:
    """List the use-case library, optionally filtered by category and/or by a
    specific library (library_set_id — see list_library_sets)."""
    params = {}
    if category:
        params["category"] = category
    if library_set_id is not None:
        params["library_set_id"] = library_set_id
    return _get("/use-case-library/", params or None)


@mcp.tool()
def list_lookups() -> dict[str, list[dict]]:
    """List the global lookup lists: project statuses, feature types, use-case
    statuses, contact roles, and task statuses/priorities. Use these names with
    the write tools."""
    return {
        "project_statuses": _get("/project-statuses/"),
        "feature_types": _get("/feature-types/"),
        "use_case_statuses": _get("/use-case-statuses/"),
        "contact_roles": _get("/contact-roles/"),
        "task_statuses": _get("/task-statuses/"),
        "task_priorities": _get("/task-priorities/"),
    }


# ---------------------------------------------------------------------------
# Write tools — customers & projects
# ---------------------------------------------------------------------------


@mcp.tool()
def create_customer(
    name: str, website: str | None = None, notes: str | None = None
) -> dict:
    """Create a customer (prospect). Returns the created customer."""
    body = {"name": name, "website": website, "notes": notes}
    return _post("/customers/", {k: v for k, v in body.items() if v is not None})


@mcp.tool()
def create_project(
    customer_id: int,
    name: str | None = None,
    status: Any = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sales_engineer_id: int | None = None,
    account_executive: str | None = None,
    account_executive_email: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create a POC project for a customer. `status` may be a project-status name
    or id (defaults to the first status if omitted). Dates are ISO (YYYY-MM-DD)."""
    body: dict[str, Any] = {
        "customer_id": customer_id,
        "name": name,
        "status_id": _resolve(status, _name_map("/project-statuses/"), "project status")
        if status is not None
        else None,
        "start_date": start_date,
        "end_date": end_date,
        "sales_engineer_id": sales_engineer_id,
        "account_executive": account_executive,
        "account_executive_email": account_executive_email,
        "notes": notes,
    }
    return _post("/projects/", {k: v for k, v in body.items() if v is not None})


# ---------------------------------------------------------------------------
# Write tools — use cases (the headline: bulk-add from a list)
# ---------------------------------------------------------------------------


def _uc_payload(
    item: dict[str, Any],
    status_map: dict[str, int],
    feature_map: dict[str, int],
) -> dict[str, Any]:
    """Build a project-use-case POST/PATCH payload from a loose item dict."""
    body: dict[str, Any] = {
        "name": item.get("name"),
        "category": item.get("category"),
        "reference_number": item.get("reference_number"),
        "description": item.get("description"),
        "success_validation": item.get("success_validation"),
        "comments": item.get("comments"),
    }
    ft = item.get("feature_type_id", item.get("feature_type"))
    body["feature_type_id"] = _resolve(ft, feature_map, "feature type")
    st = item.get("status_id", item.get("status"))
    body["status_id"] = _resolve(st, status_map, "use-case status")
    return {k: v for k, v in body.items() if v is not None}


@mcp.tool()
def add_custom_use_cases(project_id: int, use_cases: list[dict]) -> dict:
    """Bulk-add ad-hoc (custom) use cases to a project from a list.

    This is the main tool for taking a list of use cases provided in a
    conversation and pushing them into an existing POC.

    Each item is an object with:
      - name (required), category (required)
      - reference_number  (e.g. "1.1" — optional, per-project ordering)
      - description, success_validation, comments  (optional)
      - feature_type  (name or id, optional — e.g. "JML", "ISPM")
      - status        (name or id, optional — defaults to "Pending Testing")

    Returns a summary: how many were added, the created items, and any per-item
    errors (the rest still get added).
    """
    status_map = _name_map("/use-case-statuses/")
    feature_map = _name_map("/feature-types/")
    created: list[dict] = []
    errors: list[dict] = []
    for i, item in enumerate(use_cases):
        try:
            if not item.get("name") or not item.get("category"):
                raise ValueError("each use case needs at least 'name' and 'category'")
            payload = _uc_payload(item, status_map, feature_map)
            res = _post(f"/projects/{project_id}/use-cases", payload)
            created.append({
                "id": res["id"],
                "name": res["name"],
                "category": res["category"],
                "reference_number": res.get("reference_number"),
            })
        except Exception as exc:
            errors.append({"index": i, "name": item.get("name"), "error": str(exc)})
    return {
        "project_id": project_id,
        "added": len(created),
        "created": created,
        "errors": errors,
    }


@mcp.tool()
def add_custom_use_case(
    project_id: int,
    name: str,
    category: str,
    reference_number: str | None = None,
    description: str | None = None,
    success_validation: str | None = None,
    feature_type: Any = None,
    status: Any = None,
    comments: str | None = None,
) -> dict:
    """Add a single ad-hoc (custom) use case to a project. `feature_type` and
    `status` accept a name or id. Returns the created use case."""
    payload = _uc_payload(
        {
            "name": name, "category": category, "reference_number": reference_number,
            "description": description, "success_validation": success_validation,
            "feature_type": feature_type, "status": status, "comments": comments,
        },
        _name_map("/use-case-statuses/"),
        _name_map("/feature-types/"),
    )
    return _post(f"/projects/{project_id}/use-cases", payload)


@mcp.tool()
def add_use_cases_from_library(project_id: int, library_ids: list[int]) -> list[dict]:
    """Copy library use cases into a project as snapshots (de-duplicated — entries
    already on the project are skipped). Returns the use cases that were added."""
    return _post(
        f"/projects/{project_id}/use-cases/from-library", {"library_ids": library_ids}
    )


@mcp.tool()
def update_use_case(
    use_case_id: int,
    name: str | None = None,
    category: str | None = None,
    reference_number: str | None = None,
    description: str | None = None,
    success_validation: str | None = None,
    feature_type: Any = None,
    status: Any = None,
    comments: str | None = None,
) -> dict:
    """Update fields on an existing project use case. Only provided fields change.
    `feature_type` and `status` accept a name or id. Returns the updated use case."""
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if category is not None:
        body["category"] = category
    if reference_number is not None:
        body["reference_number"] = reference_number
    if description is not None:
        body["description"] = description
    if success_validation is not None:
        body["success_validation"] = success_validation
    if comments is not None:
        body["comments"] = comments
    if feature_type is not None:
        body["feature_type_id"] = _resolve(
            feature_type, _name_map("/feature-types/"), "feature type"
        )
    if status is not None:
        body["status_id"] = _resolve(
            status, _name_map("/use-case-statuses/"), "use-case status"
        )
    return _patch(f"/projects/use-cases/{use_case_id}", body)


@mcp.tool()
def set_use_case_status(use_case_id: int, status: Any) -> dict:
    """Set a use case's status (by name, e.g. "Completed", or id). Returns it."""
    status_id = _resolve(status, _name_map("/use-case-statuses/"), "use-case status")
    return _patch(f"/projects/use-cases/{use_case_id}", {"status_id": status_id})


@mcp.tool()
def delete_use_case(use_case_id: int) -> dict:
    """Delete a project use case (and its screenshots). Returns a confirmation."""
    _delete(f"/projects/use-cases/{use_case_id}")
    return {"deleted": True, "use_case_id": use_case_id}


# ---------------------------------------------------------------------------
# Project note tools (dated journal entries)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_notes(project_id: int) -> list[dict]:
    """List a project's dated journal notes, newest first. Each note has its
    date, text body, author, and whether it's internal-only (hidden from
    external viewers of the project)."""
    return _get(f"/projects/{project_id}/notes")


@mcp.tool()
def get_note(note_id: int) -> dict:
    """Get one project note in full (date, body, internal-only flag, attachments)."""
    return _get(f"/projects/notes/{note_id}")


@mcp.tool()
def add_note(
    project_id: int,
    body: str,
    note_date: str | None = None,
    is_internal_only: bool = False,
    created_by: str | None = None,
) -> dict:
    """Add a dated journal note to a project.

    `body` may contain limited HTML and is sanitized. `note_date` is ISO
    (YYYY-MM-DD) and defaults to today. `is_internal_only` hides the note from
    external viewers. `created_by` is a display label (defaults to this MCP
    caller). Returns the created note.
    """
    payload: dict[str, Any] = {"body": body, "is_internal_only": is_internal_only}
    if note_date is not None:
        payload["note_date"] = note_date
    if created_by is not None:
        payload["created_by"] = created_by
    return _post(f"/projects/{project_id}/notes", payload)


@mcp.tool()
def update_note(
    note_id: int,
    body: str | None = None,
    note_date: str | None = None,
    is_internal_only: bool | None = None,
) -> dict:
    """Update a project note. Only provided fields change. `body` is sanitized
    HTML; `note_date` is ISO (YYYY-MM-DD). Returns the updated note."""
    payload: dict[str, Any] = {}
    if body is not None:
        payload["body"] = body
    if note_date is not None:
        payload["note_date"] = note_date
    if is_internal_only is not None:
        payload["is_internal_only"] = is_internal_only
    return _patch(f"/projects/notes/{note_id}", payload)


@mcp.tool()
def delete_note(note_id: int) -> dict:
    """Delete a project note (and its attachments). Returns a confirmation."""
    _delete(f"/projects/notes/{note_id}")
    return {"deleted": True, "note_id": note_id}


# ---------------------------------------------------------------------------
# Task tools (per-user tasks; owner is explicit since MCP auth is machine-level)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_tasks(
    owner: str | None = None,
    status_id: int | None = None,
    priority_id: int | None = None,
    project_id: int | None = None,
    include_archived: bool = False,
) -> list[dict]:
    """List tasks across all users, newest-updated first.

    Tasks are per-user. Filter by `owner` (username or user id), `status_id`,
    `priority_id`, `project_id`, or set `include_archived` to include archived
    tasks. Returns each task with its owner, status, priority, and project.
    """
    params: dict[str, Any] = {"include_archived": include_archived}
    if owner is not None:
        params["owner"] = owner
    if status_id is not None:
        params["status_id"] = status_id
    if priority_id is not None:
        params["priority_id"] = priority_id
    if project_id is not None:
        params["project_id"] = project_id
    return _get("/tasks/", params)


@mcp.tool()
def get_task(task_id: int) -> dict:
    """Get one task in full (owner, status, priority, project, dates, details)."""
    return _get(f"/tasks/{task_id}")


@mcp.tool()
def create_task(
    owner: str,
    title: str,
    status: Any = None,
    priority: Any = None,
    project_id: int | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    details: str | None = None,
    is_internal_only: bool = False,
) -> dict:
    """Create a task for a user.

    `owner` (required) is the task owner — a username or user id (tasks are
    per-user, and MCP authenticates as a machine, so the owner must be explicit).
    `status` and `priority` accept a name or id (status defaults to the first
    active status). Dates are ISO (YYYY-MM-DD). `details` may contain limited
    HTML and is sanitized. `is_internal_only` hides the task from external
    viewers (default false). Returns the created task.
    """
    body: dict[str, Any] = {
        "owner": owner,
        "title": title,
        "status": status,
        "priority": priority,
        "project_id": project_id,
        "start_date": start_date,
        "due_date": due_date,
        "details": details,
        "is_internal_only": is_internal_only,
    }
    return _post("/tasks/", {k: v for k, v in body.items() if v is not None})


@mcp.tool()
def update_task(
    task_id: int,
    title: str | None = None,
    status: Any = None,
    priority: Any = None,
    owner: Any = None,
    project_id: int | None = None,
    start_date: str | None = None,
    due_date: str | None = None,
    details: str | None = None,
    is_archived: bool | None = None,
    is_internal_only: bool | None = None,
) -> dict:
    """Update a task. Only provided fields change. `status`/`priority` accept a
    name or id; `owner` (username or id) reassigns ownership. `is_internal_only`
    toggles whether the task is hidden from external viewers. Returns the task."""
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if status is not None:
        body["status"] = status
    if priority is not None:
        body["priority"] = priority
    if owner is not None:
        body["owner"] = owner
    if project_id is not None:
        body["project_id"] = project_id
    if start_date is not None:
        body["start_date"] = start_date
    if due_date is not None:
        body["due_date"] = due_date
    if details is not None:
        body["details"] = details
    if is_archived is not None:
        body["is_archived"] = is_archived
    if is_internal_only is not None:
        body["is_internal_only"] = is_internal_only
    return _patch(f"/tasks/{task_id}", body)


@mcp.tool()
def set_task_status(task_id: int, status: Any) -> dict:
    """Set a task's status (by name, e.g. "In Progress", or id). Returns the task."""
    return _patch(f"/tasks/{task_id}", {"status": status})


@mcp.tool()
def delete_task(task_id: int) -> dict:
    """Delete a task. Returns a confirmation."""
    _delete(f"/tasks/{task_id}")
    return {"deleted": True, "task_id": task_id}


# ---------------------------------------------------------------------------
# Reporting tools
# ---------------------------------------------------------------------------


@mcp.tool()
def all_pocs_summary() -> str:
    """A concise text summary of every active POC: customer, status, and
    use-case completion progress. Useful for a quick portfolio overview."""
    projects = _get("/projects/")
    if not projects:
        return "No active POC projects."
    lines = [f"{len(projects)} active POC project(s):", ""]
    for p in projects:
        detail = _get(f"/projects/{p['id']}")
        ucs = detail.get("use_cases", [])
        done = sum(1 for u in ucs if u.get("status", {}).get("name") == "Completed")
        name = p.get("name") or p["customer"]["name"]
        lines.append(
            f"- {p['customer']['name']} — {name} [{p['status']['name']}] "
            f"({done}/{len(ucs)} use cases complete)"
        )
    return "\n".join(lines)


@mcp.tool()
def project_report(project_id: int, include_internal: bool = False) -> str:
    """Generate a full text report for one POC: header, dates, people, and every
    use case grouped by category with status, comments, and validation.

    ``include_internal`` selects the audience: the default (False) is a
    client-facing report that omits internal-only journal notes; pass True for an
    internal report that includes them (each flagged ``[internal only]``)."""
    p = _get(f"/projects/{project_id}")
    name = p.get("name") or p["customer"]["name"]
    out = [
        f"# POC Report — {name}",
        f"Customer: {p['customer']['name']}",
        f"Status: {p['status']['name']}",
        f"Sales Engineer: {(p.get('sales_engineer') or {}).get('username', '—')}",
        f"Account Executive: {p.get('account_executive') or '—'}",
        f"Dates: {p.get('start_date') or '—'} → {p.get('end_date') or '—'}",
        "",
    ]
    use_cases = p.get("use_cases", [])
    by_cat: dict[str, list[dict]] = {}
    for uc in use_cases:
        by_cat.setdefault(uc["category"], []).append(uc)
    done = sum(1 for u in use_cases if u.get("status", {}).get("name") == "Completed")
    out.append(f"Use cases: {done}/{len(use_cases)} complete")
    out.append("")
    for category, ucs in sorted(by_cat.items()):
        out.append(f"## {category}")
        for uc in sorted(ucs, key=lambda u: (u.get("reference_number") or "")):
            ref = uc.get("reference_number") or "—"
            status = uc.get("status", {}).get("name", "?")
            out.append(f"- [{ref}] {uc['name']} — {status}")
            if uc.get("success_validation"):
                out.append(f"    Success: {uc['success_validation']}")
            if uc.get("comments"):
                out.append(f"    Comments: {uc['comments']}")
        out.append("")
    notes = _get(f"/projects/{project_id}/notes")
    if not include_internal:
        # Client-facing report: drop internal-only notes entirely.
        notes = [n for n in notes if not n.get("is_internal_only")]
    if notes:
        out.append("## Journal notes")
        for n in notes:
            flag = " [internal only]" if n.get("is_internal_only") else ""
            out.append(f"- {n.get('note_date') or '—'}{flag}: {n.get('body') or ''}")
        out.append("")
    return "\n".join(out)


def _send_json(send: Any, status: int, body: bytes, extra_headers: list | None = None):
    headers = [(b"content-type", b"application/json"), *(extra_headers or [])]

    async def _do() -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return _do()


class GatewayAuthMiddleware:
    """ASGI middleware enforcing inbound access control for the HTTP transports.

    Both the bearer token and the allowed-hosts list are read **live** from the
    data volume (managed in the app UI: Settings → MCP), so the MCP container
    needs no secrets at deploy time and rotation takes effect immediately:

    * No gateway token configured yet  → 503 (tell the operator to set one up).
    * Host not in a configured allow-list → 403 (empty list = allow any host).
    * Missing / wrong bearer token       → 401.

    Non-HTTP scopes (lifespan, etc.) pass straight through.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from app.services import mcp_gateway, mcp_gateway_tokens

        headers = dict(scope.get("headers") or [])

        # Host allow-list (optional hardening; empty = allow any).
        allowed = mcp_gateway.read_allowed_hosts()
        host = headers.get(b"host", b"").decode("latin-1")
        if not mcp_gateway.host_allowed(host, allowed):
            await _send_json(
                send, 403,
                b'{"error":"forbidden","detail":"Host not in POCT allowed hosts."}',
            )
            return

        # No tokens configured yet → 503 so it's safe to deploy before setup.
        if not mcp_gateway_tokens.is_configured():
            await _send_json(
                send, 503,
                b'{"error":"unconfigured","detail":"No MCP gateway token set. '
                b'Generate one in the app UI (Settings -> MCP)."}',
            )
            return

        # Presented bearer must match one of the active tokens (or the env override).
        raw = headers.get(b"authorization", b"").decode("latin-1")
        provided = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
        if not mcp_gateway_tokens.verify(provided):
            await _send_json(
                send, 401,
                b'{"error":"unauthorized","detail":"Missing or invalid bearer token."}',
                [(b"www-authenticate", b"Bearer")],
            )
            return

        await self.app(scope, receive, send)


def build_http_app(transport: str) -> Any:
    """Build the auth-wrapped ASGI app for an HTTP transport.

    DNS-rebinding protection in the SDK is disabled because this middleware does
    its own (UI-managed) host allow-listing; bearer auth is the primary control.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    )
    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    elif transport == "sse":
        app = mcp.sse_app()
    else:
        raise ValueError(f"Unknown HTTP transport: {transport!r}")
    return GatewayAuthMiddleware(app)


def main() -> None:
    """Entry point — run the MCP server using the configured transport.

    stdio is local-only. The HTTP transports are network-facing and gated by the
    UI-managed gateway token (see GatewayAuthMiddleware) — the server starts with
    no secrets and rejects calls until a token is generated in the app UI.
    """
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return
    if MCP_TRANSPORT not in ("streamable-http", "sse"):
        raise SystemExit(
            f"Unknown POCT_MCP_TRANSPORT={MCP_TRANSPORT!r} "
            "(expected: stdio, streamable-http, or sse)."
        )
    import uvicorn

    uvicorn.run(
        build_http_app(MCP_TRANSPORT),
        host=MCP_HOST,
        port=MCP_PORT,
        log_config=None,
    )


if __name__ == "__main__":
    main()
