"""AI Salesforce status update — ephemeral, date-ranged (provider mocked)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import (
    AIProvider,
    Customer,
    Project,
    ProjectNote,
    ProjectStatus,
    ProjectUseCase,
    UseCaseStatus,
)
from app.services.secret_box import encrypt_secret


def _login(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def admin_ui(client: TestClient) -> TestClient:
    from app.config import get_settings

    s = get_settings()
    _login(client, s.initial_admin_username, s.initial_admin_password)
    return client


def _make_project(name: str) -> int:
    db = get_session_factory()()
    try:
        customer = Customer(name=f"Cust {name}")
        db.add(customer)
        db.flush()
        status = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        project = Project(customer_id=customer.id, name=name, status_id=status.id)
        db.add(project)
        db.commit()
        return project.id
    finally:
        db.close()


def _add_note(pid: int, note_date: date, body: str) -> None:
    db = get_session_factory()()
    try:
        db.add(ProjectNote(project_id=pid, note_date=note_date, body=body))
        db.commit()
    finally:
        db.close()


def _add_complete_use_case(pid: int, name: str, completed_on: date | None) -> None:
    """Add a use case in a completed status, optionally without a completion date."""
    db = get_session_factory()()
    try:
        status = (
            db.query(UseCaseStatus)
            .filter(UseCaseStatus.is_complete_status.is_(True))
            .first()
        )
        db.add(
            ProjectUseCase(
                project_id=pid,
                category="Auth",
                name=name,
                status_id=status.id,
                completed_on=completed_on,
            )
        )
        db.commit()
    finally:
        db.close()


def _add_provider() -> None:
    db = get_session_factory()()
    try:
        db.add(
            AIProvider(
                provider="anthropic",
                display_name="Claude",
                model="claude-opus-4-8",
                api_key_encrypted=encrypt_secret("sk-test"),
                is_enabled=True,
                is_default=True,
            )
        )
        db.commit()
    finally:
        db.close()


def _patch_stream(monkeypatch: pytest.MonkeyPatch, fn) -> dict:
    """Replace the anthropic provider spec with one whose ``stream`` is ``fn``."""
    from app.services.ai import registry

    monkeypatch.setitem(
        registry.PROVIDERS,
        "anthropic",
        registry.PROVIDERS["anthropic"].__class__(
            key="anthropic", label="Anthropic (Claude)",
            default_model="claude-opus-4-8", suggested_models=["claude-opus-4-8"],
            implemented=True, stream=fn,
        ),
    )
    return {}


def test_salesforce_update_streams_range_activity(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_project("Ranged POC")
    _add_provider()
    today = date.today()
    _add_note(pid, today - timedelta(days=2), "Completed SSO integration testing.")
    _add_note(pid, today - timedelta(days=40), "Kickoff call held with the customer.")

    captured: dict = {}

    def fake_stream(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        captured["prompt"] = prompt
        captured["api_key"] = api_key
        yield "Good progress this week. "
        yield "SSO testing wrapped up."

    _patch_stream(monkeypatch, fake_stream)

    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
    )
    assert resp.status_code == 200
    assert "SSO testing wrapped up." in resp.text
    # The prompt is scoped to the window: in-range note included, older one excluded.
    assert captured["api_key"] == "sk-test"  # decrypted before the call
    assert "Completed SSO integration testing." in captured["prompt"]
    assert "Kickoff call" not in captured["prompt"]


def test_salesforce_update_uses_updated_at_when_no_completed_on(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_project("Fallback POC")
    _add_provider()
    # Completed but never stamped with a completion date — its updated_at (now,
    # i.e. today) should place it inside a last-7-days window.
    _add_complete_use_case(pid, "SSO login works", completed_on=None)

    captured: dict = {}

    def fake_stream(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        captured["prompt"] = prompt
        yield "Wrapped up SSO."

    _patch_stream(monkeypatch, fake_stream)

    today = date.today()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
    )
    assert resp.status_code == 200
    assert "SSO login works" in captured["prompt"]
    assert "last updated" in captured["prompt"]  # flagged as a proxy date, not exact


def test_salesforce_update_without_provider_returns_400(admin_ui: TestClient) -> None:
    pid = _make_project("No Provider POC")
    today = date.today()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
    )
    assert resp.status_code == 400
    assert "provider" in resp.text.lower()


def test_salesforce_update_invalid_range_returns_400(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_project("Bad Range POC")
    _add_provider()

    def never(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        raise AssertionError("should not be called for an invalid range")
        yield  # pragma: no cover

    _patch_stream(monkeypatch, never)

    today = date.today()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": today.isoformat(), "end": (today - timedelta(days=7)).isoformat()},
    )
    assert resp.status_code == 400
    assert "on or before" in resp.text.lower()


def test_salesforce_update_no_activity_message(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_project("Quiet POC")
    _add_provider()

    def never(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        raise AssertionError("model should not be called when there's no activity")
        yield  # pragma: no cover

    _patch_stream(monkeypatch, never)

    today = date.today()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
    )
    assert resp.status_code == 200
    assert "No notes or completed use cases" in resp.text


def test_salesforce_update_failure_is_recorded(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _make_project("Failing SF POC")
    _add_provider()
    _add_note(pid, date.today(), "Something worth reporting.")

    def boom(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        from app.services.ai.base import GenerationError

        raise GenerationError("Anthropic API error: 429 rate limited")
        yield  # pragma: no cover

    _patch_stream(monkeypatch, boom)

    today = date.today()
    resp = admin_ui.post(
        f"/ui/projects/{pid}/salesforce-update/stream",
        data={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
    )
    assert resp.status_code == 200  # stream started, error surfaced inline
    assert "429 rate limited" in resp.text

    events = json.loads(admin_ui.get("/ui/activity/export.json?category=project").text)
    failures = [e for e in events if e["event_type"] == "salesforce_update.failed"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"
    assert "429 rate limited" in failures[0]["detail"]["error"]
