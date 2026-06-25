"""REST API tests for the POC domain: lookups, customers, projects, use cases."""

from __future__ import annotations

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def test_lookup_crud_and_system_protection(api_client: TestClient) -> None:
    # Create
    r = api_client.post("/api/v1/contact-roles/", json={"name": "Integration Lead"})
    assert r.status_code == 201, r.text
    role_id = r.json()["id"]

    # Duplicate name -> 409
    assert api_client.post("/api/v1/contact-roles/", json={"name": "Integration Lead"}).status_code == 409

    # Update
    r = api_client.patch(f"/api/v1/contact-roles/{role_id}", json={"is_active": False})
    assert r.json()["is_active"] is False

    # Delete custom row -> 204
    assert api_client.delete(f"/api/v1/contact-roles/{role_id}").status_code == 204

    # System (seeded) rows can't be deleted -> 409
    seeded = next(
        x for x in api_client.get("/api/v1/contact-roles/").json() if x["is_system"]
    )
    assert api_client.delete(f"/api/v1/contact-roles/{seeded['id']}").status_code == 409


def test_project_status_in_use_blocks_delete(api_client: TestClient) -> None:
    # A status referenced by a project can't be deleted.
    statuses = api_client.get("/api/v1/project-statuses/").json()
    cust = api_client.post("/api/v1/customers/", json={"name": "RefCo"}).json()
    used_status = statuses[0]
    api_client.post(
        "/api/v1/projects/",
        json={"customer_id": cust["id"], "status_id": used_status["id"]},
    )
    # Seeded statuses are system rows anyway; make a deletable one that's in use.
    new_status = api_client.post(
        "/api/v1/project-statuses/", json={"name": "Temp Status", "sort_order": 5}
    ).json()
    api_client.post(
        "/api/v1/projects/",
        json={"customer_id": cust["id"], "status_id": new_status["id"]},
    )
    r = api_client.delete(f"/api/v1/project-statuses/{new_status['id']}")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Customers & contacts
# ---------------------------------------------------------------------------


def test_customer_with_contacts(api_client: TestClient) -> None:
    cust = api_client.post("/api/v1/customers/", json={"name": "Contoso"}).json()
    role = api_client.get("/api/v1/contact-roles/").json()[0]
    r = api_client.post(
        f"/api/v1/customers/{cust['id']}/contacts",
        json={"name": "Dana Lee", "email": "dana@contoso.test", "role_id": role["id"]},
    )
    assert r.status_code == 201
    detail = api_client.get(f"/api/v1/customers/{cust['id']}").json()
    assert detail["contacts"][0]["name"] == "Dana Lee"
    assert detail["contacts"][0]["role"]["name"] == role["name"]


def test_cannot_delete_customer_with_projects(api_client: TestClient) -> None:
    cust = api_client.post("/api/v1/customers/", json={"name": "Stark Industries"}).json()
    api_client.post("/api/v1/projects/", json={"customer_id": cust["id"]})
    assert api_client.delete(f"/api/v1/customers/{cust['id']}").status_code == 409


# ---------------------------------------------------------------------------
# Projects & use cases (the snapshot model)
# ---------------------------------------------------------------------------


def test_add_from_library_is_a_snapshot(api_client: TestClient) -> None:
    cust = api_client.post("/api/v1/customers/", json={"name": "Wayne Enterprises"}).json()
    proj = api_client.post("/api/v1/projects/", json={"customer_id": cust["id"]}).json()

    lib = api_client.get("/api/v1/use-case-library/").json()
    lib_ids = [lib[0]["id"], lib[1]["id"]]
    created = api_client.post(
        f"/api/v1/projects/{proj['id']}/use-cases/from-library",
        json={"library_ids": lib_ids},
    ).json()
    assert len(created) == 2

    # Re-adding the same ids plus a new one is de-duplicated.
    again = api_client.post(
        f"/api/v1/projects/{proj['id']}/use-cases/from-library",
        json={"library_ids": [*lib_ids, lib[2]["id"]]},
    ).json()
    assert len(again) == 1

    # Editing the library entry does NOT change the project's snapshot.
    api_client.patch(f"/api/v1/use-case-library/{lib[0]['id']}", json={"name": "RENAMED"})
    detail = api_client.get(f"/api/v1/projects/{proj['id']}").json()
    snapshot = next(u for u in detail["use_cases"] if u["library_id"] == lib[0]["id"])
    assert snapshot["name"] != "RENAMED"
    assert snapshot["source"] == "library"


def test_custom_use_case_and_status_update(api_client: TestClient) -> None:
    cust = api_client.post("/api/v1/customers/", json={"name": "Cyberdyne"}).json()
    proj = api_client.post("/api/v1/projects/", json={"customer_id": cust["id"]}).json()
    uc = api_client.post(
        f"/api/v1/projects/{proj['id']}/use-cases",
        json={"category": "Adhoc", "name": "Customer requirement", "reference_number": "1.1"},
    ).json()
    assert uc["source"] == "custom"

    completed = next(
        s for s in api_client.get("/api/v1/use-case-statuses/").json()
        if s["is_complete_status"]
    )
    r = api_client.patch(
        f"/api/v1/projects/use-cases/{uc['id']}",
        json={"status_id": completed["id"], "comments": "Validated in demo"},
    )
    assert r.json()["status"]["name"] == completed["name"]
    assert r.json()["comments"] == "Validated in demo"


def test_deleting_library_entry_keeps_project_use_case(api_client: TestClient) -> None:
    cust = api_client.post("/api/v1/customers/", json={"name": "Umbrella"}).json()
    proj = api_client.post("/api/v1/projects/", json={"customer_id": cust["id"]}).json()
    lib = api_client.get("/api/v1/use-case-library/").json()[0]
    api_client.post(
        f"/api/v1/projects/{proj['id']}/use-cases/from-library",
        json={"library_ids": [lib["id"]]},
    )
    assert api_client.delete(f"/api/v1/use-case-library/{lib['id']}").status_code == 204
    detail = api_client.get(f"/api/v1/projects/{proj['id']}").json()
    assert len(detail["use_cases"]) == 1
    # Provenance link is nulled, snapshot content remains.
    assert detail["use_cases"][0]["library_id"] is None


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/projects/").status_code == 401
