"""MCP server for POC Tracker.

Exposes tools so an AI assistant can query POC data, generate reports, and make
changes — in particular, take a list of use cases from a conversation and push
them into a project (requirement: "MCP Server to allow for AI to contact the
platform to read from and then generate reports or query data").

It talks to the running app's REST API using an API key, so it inherits the
app's auth and stays decoupled from the database. Configure with:

    POCT_MCP_BASE_URL   base URL of the running app (default http://localhost:8010)
    POCT_MCP_API_KEY    an API key generated in the app (Settings → API Keys)

Run it (stdio transport, for Claude Desktop / MCP clients):

    poct-mcp
    # or
    python -m app.mcp_server

The write tools mutate data using the API key's permissions. Lookups (status,
feature type) may be passed by name or id — names are resolved case-insensitively.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("POCT_MCP_BASE_URL", "http://localhost:8010").rstrip("/")
API_KEY = os.environ.get("POCT_MCP_API_KEY", "")

mcp = FastMCP("poc-tracker")

# Lazily-created HTTP session. Tests inject a TestClient here.
_session: httpx.Client | None = None


def _http() -> httpx.Client:
    global _session
    if _session is None:
        if not API_KEY:
            raise RuntimeError(
                "POCT_MCP_API_KEY is not set. Generate an API key in the app "
                "(Settings → API Keys) and export it for the MCP server."
            )
        _session = httpx.Client(
            base_url=f"{BASE_URL}/api/v1",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=30.0,
        )
    return _session


def _request(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Call the REST API and return parsed JSON, raising a clean error on failure."""
    resp = _http().request(method, path, json=json, params=params)
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
def list_use_case_library(category: str | None = None) -> list[dict]:
    """List the master use-case library, optionally filtered by category."""
    params = {"category": category} if category else None
    return _get("/use-case-library/", params)


@mcp.tool()
def list_lookups() -> dict[str, list[dict]]:
    """List the global lookup lists: project statuses, feature types,
    use-case statuses, and contact roles. Use these names with the write tools."""
    return {
        "project_statuses": _get("/project-statuses/"),
        "feature_types": _get("/feature-types/"),
        "use_case_statuses": _get("/use-case-statuses/"),
        "contact_roles": _get("/contact-roles/"),
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
def project_report(project_id: int) -> str:
    """Generate a full text report for one POC: header, dates, people, and every
    use case grouped by category with status, comments, and validation."""
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
    return "\n".join(out)


def main() -> None:
    """Entry point — run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
