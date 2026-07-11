"""AI provider settings + executive-summary generation (provider mocked)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AIProvider, Customer, Project, ProjectStatus
from app.services.secret_box import decrypt_secret


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


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------


def test_add_ai_provider_encrypts_key_and_sets_default(admin_ui: TestClient) -> None:
    resp = admin_ui.post(
        "/ui/settings/ai/new",
        data={
            "provider": "anthropic",
            "display_name": "Claude",
            "model": "claude-opus-4-8",
            "api_key": "sk-ant-secret",
            "is_enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        row = db.query(AIProvider).one()
        assert row.provider == "anthropic"
        assert row.is_default is True  # first provider auto-defaults
        assert row.api_key_encrypted and row.api_key_encrypted != "sk-ant-secret"
        assert decrypt_secret(row.api_key_encrypted) == "sk-ant-secret"
    finally:
        db.close()


def test_unimplemented_provider_rejected(admin_ui: TestClient) -> None:
    resp = admin_ui.post(
        "/ui/settings/ai/new",
        data={"provider": "openai", "model": "gpt-x", "api_key": "k"},
        follow_redirects=False,
    )
    assert resp.status_code == 200  # re-rendered form with error
    assert "supported provider" in resp.text.lower()
    db = get_session_factory()()
    try:
        assert db.query(AIProvider).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Generation (provider call mocked — no network, no real key)
# ---------------------------------------------------------------------------


def _add_provider() -> None:
    db = get_session_factory()()
    try:
        from app.services.secret_box import encrypt_secret

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


def test_generate_exec_summary(admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = _make_project("H-E-B POC")
    _add_provider()

    captured = {}

    def fake_generate(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        captured["api_key"] = api_key
        captured["model"] = model
        captured["prompt"] = prompt
        if usage is not None:
            usage.update(input_tokens=900, output_tokens=300, total_tokens=1200)
        return "The POC is going well.\n\nAll core use cases passed."

    # Patch the registry's anthropic spec generate function.
    from app.services.ai import registry

    monkeypatch.setitem(
        registry.PROVIDERS,
        "anthropic",
        registry.PROVIDERS["anthropic"].__class__(
            key="anthropic", label="Anthropic (Claude)",
            default_model="claude-opus-4-8", suggested_models=["claude-opus-4-8"],
            implemented=True, generate=fake_generate,
        ),
    )

    resp = admin_ui.post(
        f"/ui/projects/{pid}/exec-summary/generate", follow_redirects=False
    )
    assert resp.status_code == 303

    assert captured["api_key"] == "sk-test"  # decrypted before the call
    assert captured["model"] == "claude-opus-4-8"
    assert "H-E-B POC" in captured["prompt"]  # project context built into prompt

    db = get_session_factory()()
    try:
        project = db.get(Project, pid)
        assert project.exec_summary == "The POC is going well.\n\nAll core use cases passed."
        assert "<p>" in project.exec_summary_html
        assert project.exec_summary_model == "anthropic/claude-opus-4-8"
        assert project.exec_summary_generated_at is not None
        assert project.exec_summary_tokens == 1200  # usage captured
    finally:
        db.close()

    # The summary (and its token usage) renders on the project page.
    page = admin_ui.get(f"/ui/projects/{pid}").text
    assert "Executive Summary" in page
    assert "1,200 tokens" in page
    assert "going well" in admin_ui.get(f"/ui/reports/projects/{pid}").text


def test_generate_failure_is_recorded(
    admin_ui: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    pid = _make_project("Failing POC")
    _add_provider()

    def boom(*, api_key, model, system, prompt, max_tokens=1500, usage=None):
        from app.services.ai.base import GenerationError

        raise GenerationError("Anthropic API error: 429 rate limited")

    from app.services.ai import registry

    monkeypatch.setitem(
        registry.PROVIDERS,
        "anthropic",
        registry.PROVIDERS["anthropic"].__class__(
            key="anthropic", label="Anthropic (Claude)",
            default_model="claude-opus-4-8", suggested_models=["claude-opus-4-8"],
            implemented=True, generate=boom,
        ),
    )

    resp = admin_ui.post(
        f"/ui/projects/{pid}/exec-summary/generate", follow_redirects=False
    )
    assert resp.status_code == 303  # redirects back, no crash

    events = json.loads(admin_ui.get("/ui/activity/export.json?category=project").text)
    failures = [e for e in events if e["event_type"] == "exec_summary.failed"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"
    assert "429 rate limited" in failures[0]["detail"]["error"]


def test_generate_without_provider_flashes_error(admin_ui: TestClient) -> None:
    pid = _make_project("No Provider POC")
    resp = admin_ui.post(
        f"/ui/projects/{pid}/exec-summary/generate", follow_redirects=False
    )
    assert resp.status_code == 303  # redirects back, no crash
    db = get_session_factory()()
    try:
        assert db.get(Project, pid).exec_summary is None
    finally:
        db.close()
