"""Phase 2 AI features: requirements importer + streaming exec summary (mocked)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_session_factory
from app.models import AIProvider, Customer, Project, ProjectStatus, ProjectUseCase
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


def _add_provider() -> None:
    db = get_session_factory()()
    try:
        db.add(
            AIProvider(
                provider="anthropic", display_name="Claude",
                model="claude-opus-4-8", api_key_encrypted=encrypt_secret("sk-test"),
                is_enabled=True, is_default=True,
            )
        )
        db.commit()
    finally:
        db.close()


def _install_fake_anthropic(monkeypatch, *, generate=None, stream=None) -> None:
    from app.services.ai import registry

    base = registry.PROVIDERS["anthropic"]
    monkeypatch.setitem(
        registry.PROVIDERS,
        "anthropic",
        base.__class__(
            key="anthropic", label="Anthropic (Claude)",
            default_model="claude-opus-4-8", suggested_models=["claude-opus-4-8"],
            implemented=True, generate=generate, stream=stream,
        ),
    )


# ---------------------------------------------------------------------------
# Extraction parsing (unit)
# ---------------------------------------------------------------------------


def test_parse_tolerates_fences_and_prose() -> None:
    from app.services.ai.extraction import _parse

    raw = (
        "Sure, here are the use cases:\n```json\n"
        '[{"reference_number": "1.1", "category": "Auth", "name": "SSO login", '
        '"description": "Users log in via Okta.", "success_validation": "Login works"},'
        '{"name": "Provisioning", "category": "Lifecycle"}]\n```\nLet me know!'
    )
    out = _parse(raw)
    assert len(out) == 2
    assert out[0].reference_number == "1.1"
    assert out[0].name == "SSO login"
    assert out[1].category == "Lifecycle"  # missing fields default sensibly


def test_parse_rejects_non_list() -> None:
    from app.services.ai.base import GenerationError
    from app.services.ai.extraction import _parse

    with pytest.raises(GenerationError):
        _parse("I couldn't find any requirements.")


def test_parse_recovers_truncated_array() -> None:
    """A JSON array cut off mid-object (hit the token limit) still yields the
    complete leading objects instead of failing with 'nothing back'."""
    from app.services.ai.extraction import _parse

    items = [
        {"reference_number": f"1.{i}", "category": "Access", "name": f"UC {i}"}
        for i in range(20)
    ]
    full = json.dumps(items)
    truncated = full[:400]  # cut somewhere mid-array
    out = _parse(truncated)
    assert 0 < len(out) < 20  # recovered the complete ones, dropped the partial
    assert out[0].name == "UC 0"


def test_extraction_prefers_streaming(monkeypatch) -> None:
    """When the provider supports streaming, extraction streams (avoids timeouts)."""
    from app.services.ai import registry

    used = {"stream": False, "generate": False}

    def fake_stream(*, api_key, model, system, prompt, max_tokens, documents=None):
        used["stream"] = True
        yield '[{"name":"Streamed UC","category":"X"}]'

    def fake_generate(*, api_key, model, system, prompt, max_tokens, documents=None):
        used["generate"] = True
        return "[]"

    spec = registry.PROVIDERS["anthropic"].__class__(
        key="anthropic", label="C", default_model="m", suggested_models=[],
        implemented=True, generate=fake_generate, stream=fake_stream,
    )
    from app.services.ai.extraction import _collect

    raw = _collect(spec, api_key="k", model="m", system="s", prompt="p", max_tokens=100)
    assert used["stream"] and not used["generate"]
    assert "Streamed UC" in raw


# ---------------------------------------------------------------------------
# Importer flow (extract → preview → create), provider mocked
# ---------------------------------------------------------------------------


def test_import_extract_and_create(admin_ui: TestClient, monkeypatch) -> None:
    pid = _make_project("H-E-B POC")
    _add_provider()

    def fake_generate(*, api_key, model, system, prompt, max_tokens=8000, documents=None):
        assert "Okta" in prompt  # the requirements text reached the model
        assert documents is None  # text path sends no native documents
        return (
            '[{"reference_number":"1.1","category":"Access","name":"SSO via Okta",'
            '"description":"Okta SSO","success_validation":"login ok"},'
            '{"reference_number":"1.2","category":"Access","name":"MFA","description":"",'
            '"success_validation":""}]'
        )

    _install_fake_anthropic(monkeypatch, generate=fake_generate)

    # Extract → preview page lists candidates.
    preview = admin_ui.post(
        f"/ui/projects/{pid}/import/extract",
        data={"text": "1.1 SSO via Okta. 1.2 MFA required."},
    )
    assert preview.status_code == 200
    assert "SSO via Okta" in preview.text
    assert "MFA" in preview.text

    # Import only the first candidate.
    resp = admin_ui.post(
        f"/ui/projects/{pid}/import",
        data={
            "select": ["0"],
            "ref_0": "1.1", "category_0": "Access", "name_0": "SSO via Okta",
            "desc_0": "Okta SSO", "sv_0": "login ok",
            # field for an unselected row is present but not in `select`
            "ref_1": "1.2", "category_1": "Access", "name_1": "MFA",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = get_session_factory()()
    try:
        ucs = db.query(ProjectUseCase).filter(ProjectUseCase.project_id == pid).all()
        assert len(ucs) == 1
        assert ucs[0].name == "SSO via Okta"
        assert ucs[0].category == "Access"
        assert ucs[0].reference_number == "1.1"
    finally:
        db.close()


def test_import_native_pdf_and_dedup_context(admin_ui: TestClient, monkeypatch) -> None:
    """A PDF upload is sent natively; existing use cases are passed for dedup."""
    pid = _make_project("Native POC")
    _add_provider()

    # Seed an existing use case so the dedup context appears in the prompt.
    db = get_session_factory()()
    try:
        status = db.query(ProjectStatus).order_by(ProjectStatus.sort_order).first()
        db.add(
            ProjectUseCase(
                project_id=pid, source="custom", category="Access",
                name="Existing SSO", reference_number="1.1",
                status_id=status.id,
            )
        )
        db.commit()
    finally:
        db.close()

    captured = {}

    def fake_generate(*, api_key, model, system, prompt, max_tokens=8000, documents=None):
        captured["documents"] = documents
        captured["prompt"] = prompt
        return '[{"reference_number":"2.1","category":"Reporting","name":"Dashboards"}]'

    _install_fake_anthropic(monkeypatch, generate=fake_generate)

    resp = admin_ui.post(
        f"/ui/projects/{pid}/import/extract",
        files={"file": ("reqs.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
        data={"text": ""},
    )
    assert resp.status_code == 200

    # The PDF was passed natively (base64), not flattened to text.
    assert captured["documents"] and captured["documents"][0]["media_type"] == "application/pdf"
    assert captured["documents"][0]["data"]  # base64 payload present
    # The existing use case was included so the model can avoid duplicates.
    assert "Existing SSO" in captured["prompt"]


# ---------------------------------------------------------------------------
# Streaming exec summary, provider stream mocked
# ---------------------------------------------------------------------------


def test_stream_exec_summary_saves_at_end(admin_ui: TestClient, monkeypatch) -> None:
    pid = _make_project("Streaming POC")
    _add_provider()

    def fake_stream(*, api_key, model, system, prompt, max_tokens=1500):
        assert api_key == "sk-test"
        yield "The POC "
        yield "is going well."

    _install_fake_anthropic(monkeypatch, stream=fake_stream)

    resp = admin_ui.post(f"/ui/projects/{pid}/exec-summary/stream")
    assert resp.status_code == 200
    assert resp.text == "The POC is going well."

    db = get_session_factory()()
    try:
        project = db.get(Project, pid)
        assert project.exec_summary == "The POC is going well."
        assert "<p>" in project.exec_summary_html
        assert project.exec_summary_model == "anthropic/claude-opus-4-8"
    finally:
        db.close()


def test_stream_without_provider_returns_400(admin_ui: TestClient) -> None:
    pid = _make_project("No Provider POC")
    resp = admin_ui.post(f"/ui/projects/{pid}/exec-summary/stream")
    assert resp.status_code == 400
