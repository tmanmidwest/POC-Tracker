"""The Google Tasks connect flow records failures to the activity log.

Regression coverage for the previously-silent failure paths: a failed connect
used to write only an app-log ``log.warning`` and show a bare "it failed" flash,
leaving no diagnosable trail. These tests drive the OAuth callback down its
no-network failure branches and assert a ``task.google_connect_failed`` event
lands in the activity log with a reason in its detail.
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


def _task_events(client: TestClient) -> list[dict]:
    resp = client.get("/ui/activity/export.json?category=task")
    assert resp.status_code == 200
    return json.loads(resp.text)


def test_invalid_state_records_connect_failure(ui_session: TestClient) -> None:
    # No prior /connect, so there is no state/verifier in the session: the
    # callback should reject it as an invalid/expired authorization response.
    resp = ui_session.get(
        "/ui/tasks/google/callback?code=abc&state=mismatch",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/tasks"

    failures = [
        e for e in _task_events(ui_session)
        if e["event_type"] == "task.google_connect_failed"
    ]
    assert len(failures) == 1
    event = failures[0]
    assert event["outcome"] == "failure"
    assert event["actor_label"] == "robbytheadmin"
    assert "invalid or expired" in event["detail"]["reason"]
    # The structured detail pinpoints which precondition failed.
    assert event["detail"]["state_matched"] is False


def test_consent_declined_records_connect_failure(ui_session: TestClient) -> None:
    resp = ui_session.get(
        "/ui/tasks/google/callback?error=access_denied",
        follow_redirects=False,
    )
    assert resp.status_code == 303

    failures = [
        e for e in _task_events(ui_session)
        if e["event_type"] == "task.google_connect_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"
    assert failures[0]["detail"]["google_error"] == "access_denied"
