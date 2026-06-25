"""Tests for the audit / activity log: recording, viewing, filtering, export.

These exercise the full stack — events are recorded by the instrumented routes,
then read back through the Activity UI and its JSON/CSV export endpoints.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def ui_session(client: TestClient) -> TestClient:
    """Log in via the HTML login form and return the client with the cookie set."""
    from app.config import get_settings

    settings = get_settings()
    resp = client.post(
        "/ui/login",
        data={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return client


def _events(client: TestClient, query: str = "") -> list[dict]:
    """Fetch events via the JSON export (respects the same filters as the page)."""
    resp = client.get(f"/ui/activity/export.json{query}")
    assert resp.status_code == 200
    return json.loads(resp.text)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_activity_requires_login(client: TestClient) -> None:
    resp = client.get("/ui/activity", follow_redirects=False)
    assert resp.status_code == 303
    assert "/ui/login" in resp.headers["location"]


def test_activity_page_renders(ui_session: TestClient) -> None:
    resp = ui_session.get("/ui/activity")
    assert resp.status_code == 200
    assert "Activity" in resp.text
    assert "Export JSON" in resp.text


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_login_success_is_recorded(ui_session: TestClient) -> None:
    rows = _events(ui_session, "?category=auth")
    types = [r["event_type"] for r in rows]
    assert "auth.login.success" in types
    success = next(r for r in rows if r["event_type"] == "auth.login.success")
    assert success["outcome"] == "success"
    assert success["actor_type"] == "user"
    assert success["actor_label"] == "robbytheadmin"


def test_failed_login_is_recorded_even_though_request_rolls_back(
    client: TestClient,
) -> None:
    # A failed login re-renders the form (no redirect) and the request commits
    # nothing — the audit write must still persist via its own session.
    client.post(
        "/ui/login",
        data={"username": "robbytheadmin", "password": "wrong"},
        follow_redirects=False,
    )
    # Now log in for real so we can read the log back.
    from app.config import get_settings

    settings = get_settings()
    client.post(
        "/ui/login",
        data={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
        follow_redirects=False,
    )
    failures = _events(client, "?outcome=failure")
    assert any(r["event_type"] == "auth.login.failure" for r in failures)


def test_api_write_is_recorded_with_principal_actor(api_client: TestClient) -> None:
    # api_client authenticates with an API key; the created event should be
    # attributed to that api_key principal.
    resp = api_client.post(
        "/api/v1/contact-roles/", json={"name": "Zedland Role", "is_active": True}
    )
    assert resp.status_code == 201

    # api_client carries a Bearer header; the activity endpoints are session-auth,
    # but the same client also logged in via session for the api_key fixture chain.
    rows = _events(api_client, "?category=lookup")
    created = [r for r in rows if r["event_type"] == "lookup.contact_role.created"]
    assert created, "expected a lookup.contact_role.created event"
    event = created[0]
    assert event["actor_type"] == "api_key"
    assert event["target_label"] == "Zedland Role"
    assert event["detail"].get("surface") == "api"


def test_oauth_token_denied_is_recorded(client: TestClient) -> None:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "does-not-exist",
            "client_secret": "nope",
        },
    )
    assert resp.status_code == 401
    # Log in to read the activity log.
    from app.config import get_settings

    settings = get_settings()
    client.post(
        "/ui/login",
        data={
            "username": settings.initial_admin_username,
            "password": settings.initial_admin_password,
        },
        follow_redirects=False,
    )
    denied = _events(client, "?category=oauth")
    assert any(r["event_type"] == "oauth.token.denied" for r in denied)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_category_filter(ui_session: TestClient) -> None:
    # Generate a non-auth event.
    ui_session.post("/ui/settings/api-keys/new", data={"name": "Filter Key"})
    auth_only = _events(ui_session, "?category=auth")
    assert auth_only and all(r["category"] == "auth" for r in auth_only)
    keys_only = _events(ui_session, "?category=api_key")
    assert keys_only and all(r["category"] == "api_key" for r in keys_only)


def test_view_groups_identity_categories(ui_session: TestClient) -> None:
    ui_session.post("/ui/settings/api-keys/new", data={"name": "Grouped Key"})
    identity = _events(ui_session, "?view=identity")
    assert identity, "identity view should include the login event"
    assert all(r["category"] in ("auth", "oauth", "oidc") for r in identity)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_json_headers(ui_session: TestClient) -> None:
    resp = ui_session.get("/ui/activity/export.json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]
    assert isinstance(json.loads(resp.text), list)


def test_export_csv_headers(ui_session: TestClient) -> None:
    resp = ui_session.get("/ui/activity/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    # Header row present
    assert resp.text.splitlines()[0].startswith("occurred_at,")


# ---------------------------------------------------------------------------
# Reset integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_default_is_30_days() -> None:
    from app.config import get_settings

    assert get_settings().audit_retention_days == 30


def test_prune_deletes_only_events_older_than_window(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import get_session_factory
    from app.models.audit_event import AuditEvent
    from app.services.audit import prune_old_events

    session = get_session_factory()()
    session.add(
        AuditEvent(
            occurred_at=datetime.now(UTC) - timedelta(days=40),
            category="system",
            event_type="system.old",
            outcome="success",
            actor_type="system",
            message="old",
        )
    )
    session.add(
        AuditEvent(
            occurred_at=datetime.now(UTC) - timedelta(days=5),
            category="system",
            event_type="system.recent",
            outcome="success",
            actor_type="system",
            message="recent",
        )
    )
    session.commit()
    session.close()

    deleted = prune_old_events(30)
    assert deleted == 1

    session = get_session_factory()()
    remaining = {e.event_type for e in session.query(AuditEvent).all()}
    session.close()
    assert "system.old" not in remaining
    assert "system.recent" in remaining


def test_prune_disabled_when_retention_is_zero(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import get_session_factory
    from app.models.audit_event import AuditEvent
    from app.services.audit import prune_old_events

    session = get_session_factory()()
    session.add(
        AuditEvent(
            occurred_at=datetime.now(UTC) - timedelta(days=400),
            category="system",
            event_type="system.ancient",
            outcome="success",
            actor_type="system",
            message="ancient",
        )
    )
    session.commit()
    session.close()

    assert prune_old_events(0) == 0  # 0 = keep forever


def test_system_settings_page_renders(ui_session: TestClient) -> None:
    resp = ui_session.get("/ui/settings/system")
    assert resp.status_code == 200
    assert "Activity log retention" in resp.text
    # The default (env) value is reflected in the form.
    assert 'value="30"' in resp.text


def test_update_retention_persists_and_takes_effect(ui_session: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import get_session_factory
    from app.models.audit_event import AuditEvent
    from app.services import system_config

    # Seed an event that is 20 days old.
    session = get_session_factory()()
    session.add(
        AuditEvent(
            occurred_at=datetime.now(UTC) - timedelta(days=20),
            category="system",
            event_type="system.aged",
            outcome="success",
            actor_type="system",
            message="aged",
        )
    )
    session.commit()
    session.close()

    # Lower retention to 7 days via the UI — should persist and prune immediately.
    resp = ui_session.post(
        "/ui/settings/system",
        data={"audit_retention_days": "7"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert system_config.current_retention_days() == 7

    session = get_session_factory()()
    types = {e.event_type for e in session.query(AuditEvent).all()}
    session.close()
    assert "system.aged" not in types  # pruned by the new 7-day window
    # The change itself is audited.
    assert "system.settings.updated" in types


def test_update_retention_rejects_invalid(ui_session: TestClient) -> None:
    resp = ui_session.post(
        "/ui/settings/system",
        data={"audit_retention_days": "-5"},
        follow_redirects=False,
    )
    assert resp.status_code == 200  # re-renders with an error, no redirect
    assert "Retention must be between" in resp.text


def test_retention_zero_disables_pruning_end_to_end(ui_session: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db import get_session_factory
    from app.models.audit_event import AuditEvent
    from app.services.audit import prune_old_events

    ui_session.post(
        "/ui/settings/system",
        data={"audit_retention_days": "0"},
        follow_redirects=False,
    )
    session = get_session_factory()()
    session.add(
        AuditEvent(
            occurred_at=datetime.now(UTC) - timedelta(days=999),
            category="system",
            event_type="system.ancient",
            outcome="success",
            actor_type="system",
            message="ancient",
        )
    )
    session.commit()
    session.close()
    # A default prune now reads 0 from the DB → no-op.
    assert prune_old_events() == 0


def test_reset_can_clear_audit_log_and_logs_itself(ui_session: TestClient) -> None:
    # Seed a couple of events first.
    ui_session.post("/ui/settings/api-keys/new", data={"name": "Pre-reset Key"})
    assert len(_events(ui_session)) > 0

    resp = ui_session.post(
        "/ui/settings/reset",
        data={"reset_audit_events": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = _events(ui_session)
    # The clear wipes everything, then records the reset event itself.
    assert [r["event_type"] for r in rows] == ["system.data_reset"]
    assert "audit events" in " ".join(rows[0]["detail"]["actions"])
