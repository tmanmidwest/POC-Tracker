"""MCP server for POC Tracker.

Exposes read-only and reporting tools so an AI assistant can query POC data and
generate reports (requirement: "MCP Server to allow for AI to contact the
platform to read from and then generate reports or query data").

It talks to the running app's REST API using an API key, so it inherits the
app's auth and stays decoupled from the database. Configure with:

    POCT_MCP_BASE_URL   base URL of the running app (default http://localhost:8000)
    POCT_MCP_API_KEY    an API key generated in the app (Settings → API Keys)

Run it (stdio transport, for Claude Desktop / MCP clients):

    poct-mcp
    # or
    python -m app.mcp_server
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("POCT_MCP_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("POCT_MCP_API_KEY", "")

mcp = FastMCP("poc-tracker")


def _client() -> httpx.Client:
    if not API_KEY:
        raise RuntimeError(
            "POCT_MCP_API_KEY is not set. Generate an API key in the app "
            "(Settings → API Keys) and export it for the MCP server."
        )
    return httpx.Client(
        base_url=f"{BASE_URL}/api/v1",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30.0,
    )


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    with _client() as client:
        resp = client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


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
    use-case statuses, and contact roles."""
    return {
        "project_statuses": _get("/project-statuses/"),
        "feature_types": _get("/feature-types/"),
        "use_case_statuses": _get("/use-case-statuses/"),
        "contact_roles": _get("/contact-roles/"),
    }


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
