"""Doc-drift guard: endpoints documented in docs/API.md must exist in the OpenAPI
schema. If a route is renamed/removed without updating the docs (or a param name
changes), this fails so the docs can't silently drift.

Keep this list in sync with docs/API.md's "Endpoints" + "Auth & credential
management" sections.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# (path, method) pairs that docs/API.md promises. Paths use FastAPI's exact
# parameter names, matching the OpenAPI schema keys.
DOCUMENTED: list[tuple[str, str]] = [
    # Customers & contacts
    ("/api/v1/customers/", "get"),
    ("/api/v1/customers/", "post"),
    ("/api/v1/customers/{customer_id}", "get"),
    ("/api/v1/customers/{customer_id}", "patch"),
    ("/api/v1/customers/{customer_id}", "delete"),
    ("/api/v1/customers/{customer_id}/contacts", "get"),
    ("/api/v1/customers/{customer_id}/contacts", "post"),
    ("/api/v1/customers/contacts/{contact_id}", "patch"),
    ("/api/v1/customers/contacts/{contact_id}", "delete"),
    # Library sets
    ("/api/v1/library-sets/", "get"),
    ("/api/v1/library-sets/", "post"),
    ("/api/v1/library-sets/{set_id}", "get"),
    ("/api/v1/library-sets/{set_id}", "patch"),
    ("/api/v1/library-sets/{set_id}", "delete"),
    # Use-case library
    ("/api/v1/use-case-library/", "get"),
    ("/api/v1/use-case-library/", "post"),
    ("/api/v1/use-case-library/{entry_id}", "get"),
    ("/api/v1/use-case-library/{entry_id}", "patch"),
    ("/api/v1/use-case-library/{entry_id}", "delete"),
    # Projects & use cases
    ("/api/v1/projects/", "get"),
    ("/api/v1/projects/", "post"),
    ("/api/v1/projects/{project_id}", "get"),
    ("/api/v1/projects/{project_id}", "patch"),
    ("/api/v1/projects/{project_id}", "delete"),
    ("/api/v1/projects/{project_id}/use-cases", "get"),
    ("/api/v1/projects/{project_id}/use-cases", "post"),
    ("/api/v1/projects/{project_id}/use-cases/from-library", "post"),
    ("/api/v1/projects/use-cases/{use_case_id}", "patch"),
    ("/api/v1/projects/use-cases/{use_case_id}", "delete"),
    # Project notes
    ("/api/v1/projects/{project_id}/notes", "get"),
    ("/api/v1/projects/{project_id}/notes", "post"),
    ("/api/v1/projects/notes/{note_id}", "get"),
    ("/api/v1/projects/notes/{note_id}", "patch"),
    ("/api/v1/projects/notes/{note_id}", "delete"),
    # Tasks
    ("/api/v1/tasks/", "get"),
    ("/api/v1/tasks/", "post"),
    ("/api/v1/tasks/{task_id}", "get"),
    ("/api/v1/tasks/{task_id}", "patch"),
    ("/api/v1/tasks/{task_id}", "delete"),
    # Lookups (one representative + the rest of the list roots)
    ("/api/v1/contact-roles/", "get"),
    ("/api/v1/contact-roles/{row_id}", "patch"),
    ("/api/v1/project-statuses/", "get"),
    ("/api/v1/feature-types/", "get"),
    ("/api/v1/use-case-statuses/", "get"),
    ("/api/v1/task-statuses/", "get"),
    ("/api/v1/task-priorities/", "get"),
    # Auth & credential management (session-authenticated)
    ("/api/v1/auth/session/login", "post"),
    ("/api/v1/auth/session/logout", "post"),
    ("/api/v1/auth/session/me", "get"),
    ("/api/v1/auth/api-keys/", "get"),
    ("/api/v1/auth/api-keys/", "post"),
    ("/api/v1/auth/api-keys/{api_key_id}", "delete"),
    ("/api/v1/auth/api-keys/{api_key_id}/revoke", "post"),
    ("/api/v1/auth/oauth-clients/", "get"),
    ("/api/v1/auth/oauth-clients/", "post"),
    ("/api/v1/auth/oauth-clients/{oauth_client_pk}", "delete"),
    ("/api/v1/auth/oauth-clients/{oauth_client_pk}/revoke", "post"),
    # OAuth token endpoint (root-level)
    ("/oauth/token", "post"),
]


def test_documented_endpoints_exist_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    missing = [
        f"{method.upper()} {path}"
        for path, method in DOCUMENTED
        if path not in paths or method not in paths[path]
    ]
    assert not missing, "docs/API.md documents endpoints missing from the API: " + ", ".join(
        missing
    )
