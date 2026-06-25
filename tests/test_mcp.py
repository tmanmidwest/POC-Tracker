"""Tests for the MCP server tools.

The MCP tools call the REST API over httpx. We inject a TestClient (whose
base_url carries the /api/v1 prefix) as the MCP session so the tools exercise
the real app end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    from app import mcp_server
    from app.db import get_session_factory
    from app.main import create_app
    from app.models import ApiKey, AppUser
    from app.services.tokens import generate_api_key, hash_token

    app = create_app()
    with TestClient(app, base_url="http://testserver/api/v1") as tc:
        # Mint an API key directly and attach it to the client.
        full, prefix = generate_api_key()
        db = get_session_factory()()
        admin = db.query(AppUser).first()
        db.add(ApiKey(name="mcp-test", key_prefix=prefix, key_hash=hash_token(full),
                      created_by_user_id=admin.id))
        db.commit()
        tc.headers.update({"Authorization": f"Bearer {full}"})
        monkeypatch.setattr(mcp_server, "_session", tc)
        yield mcp_server


def test_bulk_add_custom_use_cases(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    # Seeded sample project is id 1.
    before = len(m.get_project(1)["use_cases"])
    result = m.add_custom_use_cases(1, [
        {"name": "Bulk import access", "category": "Joiner", "reference_number": "1.5",
         "feature_type": "JML", "status": "Pending Testing"},
        {"name": "SoD policy check", "category": "Certifications",
         "description": "Validate separation-of-duties detection"},
        {"category": "Broken", "description": "missing name -> error"},  # should error
    ])
    assert result["added"] == 2
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 2
    after = m.get_project(1)["use_cases"]
    assert len(after) == before + 2
    names = {u["name"] for u in after}
    assert "Bulk import access" in names
    # name -> id resolution worked (feature type JML attached)
    bulk = next(u for u in after if u["name"] == "Bulk import access")
    assert bulk["feature_type"]["name"] == "JML"
    assert bulk["source"] == "custom"


def test_single_add_and_status_by_name(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    uc = m.add_custom_use_case(1, name="Quick check", category="Misc")
    completed = m.set_use_case_status(uc["id"], "Completed")
    assert completed["status"]["name"] == "Completed"


def test_update_use_case(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    uc = m.add_custom_use_case(1, name="To edit", category="Misc")
    updated = m.update_use_case(uc["id"], comments="done in demo", reference_number="2.2")
    assert updated["comments"] == "done in demo"
    assert updated["reference_number"] == "2.2"


def test_add_from_library_via_mcp(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    cust = m.create_customer("MCP Customer")
    proj = m.create_project(cust["id"], name="MCP POC", status="Pending Scheduling")
    lib = m.list_use_case_library()
    added = m.add_use_cases_from_library(proj["id"], [lib[0]["id"], lib[1]["id"]])
    assert len(added) == 2
    # Re-adding is de-duplicated.
    again = m.add_use_cases_from_library(proj["id"], [lib[0]["id"], lib[1]["id"]])
    assert len(again) == 0


def test_find_projects(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    found = m.find_projects("acme")
    assert any("acme" in (p["customer"]["name"].lower()) for p in found)


def test_unknown_status_name_is_a_clear_error(mcp_env) -> None:  # type: ignore[no-untyped-def]
    m = mcp_env
    result = m.add_custom_use_cases(1, [
        {"name": "Bad status", "category": "X", "status": "Nope"},
    ])
    assert result["added"] == 0
    assert "Unknown use-case status" in result["errors"][0]["error"]
